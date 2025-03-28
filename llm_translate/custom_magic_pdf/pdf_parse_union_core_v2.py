import copy
import math
import os
import re
import statistics
import time
from typing import List
from collections import OrderedDict
import logging
from pathlib import Path
import re

import cv2
import fitz
import torch
import numpy as np
from loguru import logger

from magic_pdf.config.enums import SupportedPdfParseMethod
from magic_pdf.config.ocr_content_type import BlockType, ContentType
from magic_pdf.data.dataset import Dataset, PageableData
from magic_pdf.libs.boxbase import calculate_overlap_area_in_bbox1_area_ratio, __is_overlaps_y_exceeds_threshold
from magic_pdf.libs.clean_memory import clean_memory
from magic_pdf.libs.config_reader import get_local_layoutreader_model_dir, get_llm_aided_config, get_device
from magic_pdf.libs.convert_utils import dict_to_list
from magic_pdf.libs.hash_utils import compute_md5
from magic_pdf.libs.pdf_image_tools import cut_image_to_pil_image
from magic_pdf.model.magic_model import MagicModel
from magic_pdf.post_proc.llm_aided import llm_aided_formula, llm_aided_text, llm_aided_title

try:
    import torchtext

    if torchtext.__version__ >= '0.18.0':
        torchtext.disable_torchtext_deprecation_warning()
except ImportError:
    pass

from magic_pdf.model.sub_modules.model_init import AtomModelSingleton
from magic_pdf.post_proc.para_split_v3 import para_split
from magic_pdf.pre_proc.construct_page_dict import ocr_construct_page_component_v2
from magic_pdf.pre_proc.cut_image import ocr_cut_image_and_table
from magic_pdf.pre_proc.ocr_detect_all_bboxes import ocr_prepare_bboxes_for_layout_split_v2
from magic_pdf.pre_proc.ocr_dict_merge import fill_spans_in_blocks, fix_block_spans_v2, fix_discarded_block
from magic_pdf.pre_proc.ocr_span_list_modify import get_qa_need_list_v2, remove_overlaps_low_confidence_spans, \
    remove_overlaps_min_spans, check_chars_is_overlap_in_span

import magic_pdf.pdf_parse_union_core_v2 as origin_pdf_parse_union_core_v2

os.environ['NO_ALBUMENTATIONS_UPDATE'] = '1'  # 禁止albumentations检查更新


from llm_translate.config import conf
from llm_translate.edit_distance import MinDistance

LOGGER = logging.getLogger(__name__)


def __replace_STX_ETX(text_str: str):
    """Replace \u0002 and \u0003, as these characters become garbled when extracted using pymupdf. In fact, they were originally quotation marks.
    Drawback: This issue is only observed in English text; it has not been found in Chinese text so far.

        Args:
            text_str (str): raw text

        Returns:
            _type_: replaced text
    """  # noqa: E501
    if text_str:
        s = text_str.replace('\u0002', "'")
        s = s.replace('\u0003', "'")
        return s
    return text_str


def __replace_0xfffd(text_str: str):
    """Replace \ufffd, as these characters become garbled when extracted using pymupdf."""
    if text_str:
        s = text_str.replace('\ufffd', " ")
        return s
    return text_str


# 连写字符拆分
def __replace_ligatures(text: str):
    ligatures = {
        'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬀ': 'ff', 'ﬃ': 'ffi', 'ﬄ': 'ffl', 'ﬅ': 'ft', 'ﬆ': 'st'
    }
    return re.sub('|'.join(map(re.escape, ligatures.keys())), lambda m: ligatures[m.group()], text)


def chars_to_content(span):
    # 检查span中的char是否为空
    if len(span['chars']) == 0:
        pass
        # span['content'] = ''
    elif check_chars_is_overlap_in_span(span['chars']):
        pass
    else:
        # 先给chars按char['bbox']的中心点的x坐标排序
        span['chars'] = sorted(span['chars'], key=lambda x: (x['bbox'][0] + x['bbox'][2]) / 2)

        # 求char的平均宽度
        char_width_sum = sum([char['bbox'][2] - char['bbox'][0] for char in span['chars']])
        char_avg_width = char_width_sum / len(span['chars'])

        content = ''
        for char in span['chars']:

            # 如果下一个char的x0和上一个char的x1距离超过0.25个字符宽度，则需要在中间插入一个空格
            char1 = char
            char2 = span['chars'][span['chars'].index(char) + 1] if span['chars'].index(char) + 1 < len(span['chars']) else None
            if char2 and char2['bbox'][0] - char1['bbox'][2] > char_avg_width * 0.25 and char['c'] != ' ' and char2['c'] != ' ':
                content += f"{char['c']} "
            else:
                content += char['c']

        content = __replace_ligatures(content)
        span['content'] = __replace_0xfffd(content)

    del span['chars']


LINE_STOP_FLAG = ('.', '!', '?', '。', '！', '？', ')', '）', '"', '”', ':', '：', ';', '；', ']', '】', '}', '}', '>', '》', '、', ',', '，', '-', '—', '–',)
LINE_START_FLAG = ('(', '（', '"', '“', '【', '{', '《', '<', '「', '『', '【', '[',)


def fill_char_in_spans(spans, all_chars):

    # 简单从上到下排一下序
    spans = sorted(spans, key=lambda x: x['bbox'][1])

    for char in all_chars:
        # 跳过非法bbox的char
        # x1, y1, x2, y2 = char['bbox']
        # if abs(x1 - x2) <= 0.01 or abs(y1 - y2) <= 0.01:
        #     continue

        for span in spans:
            if calculate_char_in_span(char['bbox'], span['bbox'], char['c']):
                span['chars'].append(char)
                break

    need_ocr_spans = []
    for span in spans:
        chars_to_content(span)
        # 有的span中虽然没有字但有一两个空的占位符，用宽高和content长度过滤
        if len(span['content']) * span['height'] < span['width'] * 0.5:
            # logger.info(f"maybe empty span: {len(span['content'])}, {span['height']}, {span['width']}")
            need_ocr_spans.append(span)
        del span['height'], span['width']
    return need_ocr_spans


# 使用鲁棒性更强的中心点坐标判断
def calculate_char_in_span(char_bbox, span_bbox, char, span_height_radio=0.33):
    char_center_x = (char_bbox[0] + char_bbox[2]) / 2
    char_center_y = (char_bbox[1] + char_bbox[3]) / 2
    span_center_y = (span_bbox[1] + span_bbox[3]) / 2
    span_height = span_bbox[3] - span_bbox[1]

    if (
        span_bbox[0] < char_center_x < span_bbox[2]
        and span_bbox[1] < char_center_y < span_bbox[3]
        and abs(char_center_y - span_center_y) < span_height * span_height_radio  # 字符的中轴和span的中轴高度差不能超过1/4span高度
    ):
        return True
    else:
        # 如果char是LINE_STOP_FLAG，就不用中心点判定，换一种方案（左边界在span区域内，高度判定和之前逻辑一致）
        # 主要是给结尾符号一个进入span的机会，这个char还应该离span右边界较近
        if char in LINE_STOP_FLAG:
            if (
                (span_bbox[2] - span_height) < char_bbox[0] < span_bbox[2]
                and char_center_x > span_bbox[0]
                and span_bbox[1] < char_center_y < span_bbox[3]
                and abs(char_center_y - span_center_y) < span_height * span_height_radio
            ):
                return True
        elif char in LINE_START_FLAG:
            if (
                span_bbox[0] < char_bbox[2] < (span_bbox[0] + span_height)
                and char_center_x < span_bbox[2]
                and span_bbox[1] < char_center_y < span_bbox[3]
                and abs(char_center_y - span_center_y) < span_height * span_height_radio
            ):
                return True
        else:
            return False


def remove_tilted_line(text_blocks):
    for block in text_blocks:
        remove_lines = []
        for line in block['lines']:
            cosine, sine = line['dir']
            # 计算弧度值
            angle_radians = math.atan2(sine, cosine)
            # 将弧度值转换为角度值
            angle_degrees = math.degrees(angle_radians)
            if 2 < abs(angle_degrees) < 88:
                remove_lines.append(line)
        for line in remove_lines:
            block['lines'].remove(line)


def calculate_contrast(img, img_mode) -> float:
    """
    计算给定图像的对比度。
    :param img: 图像，类型为numpy.ndarray
    :Param img_mode = 图像的色彩通道，'rgb' 或 'bgr'
    :return: 图像的对比度值
    """
    if img_mode == 'rgb':
        # 将RGB图像转换为灰度图
        gray_img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    elif img_mode == 'bgr':
        # 将BGR图像转换为灰度图
        gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError("Invalid image mode. Please provide 'rgb' or 'bgr'.")

    # 计算均值和标准差
    mean_value = np.mean(gray_img)
    std_dev = np.std(gray_img)
    # 对比度定义为标准差除以平均值（加上小常数避免除零错误）
    contrast = std_dev / (mean_value + 1e-6)
    # logger.info(f"contrast: {contrast}")
    return round(contrast, 2)


def txt_spans_extract_v2(pdf_page, spans, all_bboxes, all_discarded_blocks, lang):
    # cid用0xfffd表示，连字符拆开
    # text_blocks_raw = pdf_page.get_text('rawdict', flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)['blocks']

    # cid用0xfffd表示，连字符不拆开
    #text_blocks_raw = pdf_page.get_text('rawdict', flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)['blocks']

    # 自定义flags出现较多0xfffd，可能是pymupdf可以自行处理内置字典的pdf，不再使用
    text_blocks_raw = pdf_page.get_text('rawdict', flags=fitz.TEXTFLAGS_TEXT)['blocks']
    # text_blocks = pdf_page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)['blocks']

    # 移除所有角度不为0或90的line
    remove_tilted_line(text_blocks_raw)

    all_pymu_chars = []
    for block in text_blocks_raw:
        for line in block['lines']:
            cosine, sine = line['dir']
            if abs(cosine) < 0.9 or abs(sine) > 0.1:
                continue
            for span in line['spans']:
                all_pymu_chars.extend(span['chars'])

    # 计算所有sapn的高度的中位数
    span_height_list = []
    for span in spans:
        if span['type'] in [ContentType.InterlineEquation, ContentType.Image, ContentType.Table]:
            continue
        span_height = span['bbox'][3] - span['bbox'][1]
        span['height'] = span_height
        span['width'] = span['bbox'][2] - span['bbox'][0]
        span_height_list.append(span_height)
    if len(span_height_list) == 0:
        return spans
    else:
        median_span_height = statistics.median(span_height_list)

    useful_spans = []
    unuseful_spans = []
    # 纵向span的两个特征：1. 高度超过多个line 2. 高宽比超过某个值
    vertical_spans = []
    for span in spans:
        if span['type'] in [ContentType.InterlineEquation, ContentType.Image, ContentType.Table]:
            continue
        for block in all_bboxes + all_discarded_blocks:
            if block[7] in [BlockType.ImageBody, BlockType.TableBody, BlockType.InterlineEquation]:
                continue
            if calculate_overlap_area_in_bbox1_area_ratio(span['bbox'], block[0:4]) > 0.5:
                if span['height'] > median_span_height * 3 and span['height'] > span['width'] * 3:
                    vertical_spans.append(span)
                elif block in all_bboxes:
                    useful_spans.append(span)
                else:
                    unuseful_spans.append(span)

                break

    """垂直的span框直接用pymu的line进行填充"""
    if len(vertical_spans) > 0:
        text_blocks = pdf_page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)['blocks']
        all_pymu_lines = []
        for block in text_blocks:
            for line in block['lines']:
                all_pymu_lines.append(line)

        for pymu_line in all_pymu_lines:
            for span in vertical_spans:
                if calculate_overlap_area_in_bbox1_area_ratio(pymu_line['bbox'], span['bbox']) > 0.5:
                    for pymu_span in pymu_line['spans']:
                        span['content'] += pymu_span['text']
                    break

        for span in vertical_spans:
            if len(span['content']) == 0:
                spans.remove(span)

    """水平的span框如果没有char则用ocr进行填充"""
    new_spans = []

    for span in useful_spans + unuseful_spans:
        if span['type'] in [ContentType.Text]:
            span['chars'] = []
            new_spans.append(span)

    need_ocr_spans = fill_char_in_spans(new_spans, all_pymu_chars)

    if len(need_ocr_spans) > 0:

        # 初始化ocr模型
        atom_model_manager = AtomModelSingleton()
        ocr_model = atom_model_manager.get_atom_model(
            atom_model_name='ocr',
            ocr_show_log=False,
            det_db_box_thresh=0.3,
            lang=lang
        )

        for span in need_ocr_spans:
            # 对span的bbox截图再ocr
            span_img = cut_image_to_pil_image(span['bbox'], pdf_page, mode='cv2')

            # 计算span的对比度，低于0.20的span不进行ocr
            if calculate_contrast(span_img, img_mode='bgr') <= 0.20:
                spans.remove(span)
                continue

            ocr_res = ocr_model.ocr(span_img, det=False)
            if ocr_res and len(ocr_res) > 0:
                if len(ocr_res[0]) > 0:
                    ocr_text, ocr_score = ocr_res[0][0]
                    # logger.info(f"ocr_text: {ocr_text}, ocr_score: {ocr_score}")
                    if ocr_score > 0.5 and len(ocr_text) > 0:
                        span['content'] = ocr_text
                        span['score'] = ocr_score
                    else:
                        spans.remove(span)

    return spans


def model_init(model_name: str):
    from transformers import LayoutLMv3ForTokenClassification
    device = torch.device(get_device())

    if model_name == 'layoutreader':
        # 检测modelscope的缓存目录是否存在
        layoutreader_model_dir = get_local_layoutreader_model_dir()
        if os.path.exists(layoutreader_model_dir):
            model = LayoutLMv3ForTokenClassification.from_pretrained(
                layoutreader_model_dir
            )
        else:
            logger.warning(
                'local layoutreader model not exists, use online model from huggingface'
            )
            model = LayoutLMv3ForTokenClassification.from_pretrained(
                'hantian/layoutreader'
            )
        model.to(device).eval()
    else:
        logger.error('model name not allow')
        exit(1)
    return model


class ModelSingleton:
    _instance = None
    _models = {}

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_model(self, model_name: str):
        if model_name not in self._models:
            self._models[model_name] = model_init(model_name=model_name)
        return self._models[model_name]


def do_predict(boxes: List[List[int]], model) -> List[int]:
    from magic_pdf.model.sub_modules.reading_oreder.layoutreader.helpers import (
        boxes2inputs, parse_logits, prepare_inputs)

    inputs = boxes2inputs(boxes)
    inputs = prepare_inputs(inputs, model)
    logits = model(**inputs).logits.cpu().squeeze(0)
    return parse_logits(logits, len(boxes))


def cal_block_index(fix_blocks, sorted_bboxes):

    if sorted_bboxes is not None:
        # 使用layoutreader排序
        for block in fix_blocks:
            line_index_list = []
            if len(block['lines']) == 0:
                block['index'] = sorted_bboxes.index(block['bbox'])
            else:
                for line in block['lines']:
                    line['index'] = sorted_bboxes.index(line['bbox'])
                    line_index_list.append(line['index'])
                median_value = statistics.median(line_index_list)
                block['index'] = median_value

            # 删除图表body block中的虚拟line信息, 并用real_lines信息回填
            if block['type'] in [BlockType.ImageBody, BlockType.TableBody, BlockType.Title, BlockType.InterlineEquation]:
                if 'real_lines' in block:
                    block['virtual_lines'] = copy.deepcopy(block['lines'])
                    block['lines'] = copy.deepcopy(block['real_lines'])
                    del block['real_lines']
    else:
        # 使用xycut排序
        block_bboxes = []
        for block in fix_blocks:
            # 如果block['bbox']任意值小于0，将其置为0
            block['bbox'] = [max(0, x) for x in block['bbox']]
            block_bboxes.append(block['bbox'])

            # 删除图表body block中的虚拟line信息, 并用real_lines信息回填
            if block['type'] in [BlockType.ImageBody, BlockType.TableBody, BlockType.Title, BlockType.InterlineEquation]:
                if 'real_lines' in block:
                    block['virtual_lines'] = copy.deepcopy(block['lines'])
                    block['lines'] = copy.deepcopy(block['real_lines'])
                    del block['real_lines']

        import numpy as np

        from magic_pdf.model.sub_modules.reading_oreder.layoutreader.xycut import \
            recursive_xy_cut

        random_boxes = np.array(block_bboxes)
        np.random.shuffle(random_boxes)
        res = []
        recursive_xy_cut(np.asarray(random_boxes).astype(int), np.arange(len(block_bboxes)), res)
        assert len(res) == len(block_bboxes)
        sorted_boxes = random_boxes[np.array(res)].tolist()

        for i, block in enumerate(fix_blocks):
            block['index'] = sorted_boxes.index(block['bbox'])

        # 生成line index
        sorted_blocks = sorted(fix_blocks, key=lambda b: b['index'])
        line_inedx = 1
        for block in sorted_blocks:
            for line in block['lines']:
                line['index'] = line_inedx
                line_inedx += 1

    return fix_blocks


def insert_lines_into_block(block_bbox, line_height, page_w, page_h):
    # block_bbox是一个元组(x0, y0, x1, y1)，其中(x0, y0)是左下角坐标，(x1, y1)是右上角坐标
    x0, y0, x1, y1 = block_bbox

    block_height = y1 - y0
    block_weight = x1 - x0

    # 如果block高度小于n行正文，则直接返回block的bbox
    if line_height * 2 < block_height:
        if (
            block_height > page_h * 0.25 and page_w * 0.5 > block_weight > page_w * 0.25
        ):  # 可能是双列结构，可以切细点
            lines = int(block_height / line_height) + 1
        else:
            # 如果block的宽度超过0.4页面宽度，则将block分成3行(是一种复杂布局，图不能切的太细)
            if block_weight > page_w * 0.4:
                lines = 3
                line_height = (y1 - y0) / lines
            elif block_weight > page_w * 0.25:  # （可能是三列结构，也切细点）
                lines = int(block_height / line_height) + 1
            else:  # 判断长宽比
                if block_height / block_weight > 1.2:  # 细长的不分
                    return [[x0, y0, x1, y1]]
                else:  # 不细长的还是分成两行
                    lines = 2
                    line_height = (y1 - y0) / lines

        # 确定从哪个y位置开始绘制线条
        current_y = y0

        # 用于存储线条的位置信息[(x0, y), ...]
        lines_positions = []

        for i in range(lines):
            lines_positions.append([x0, current_y, x1, current_y + line_height])
            current_y += line_height
        return lines_positions

    else:
        return [[x0, y0, x1, y1]]


def sort_lines_by_model(fix_blocks, page_w, page_h, line_height):
    page_line_list = []

    def add_lines_to_block(b):
        line_bboxes = insert_lines_into_block(b['bbox'], line_height, page_w, page_h)
        b['lines'] = []
        for line_bbox in line_bboxes:
            b['lines'].append({'bbox': line_bbox, 'spans': []})
        page_line_list.extend(line_bboxes)

    for block in fix_blocks:
        if block['type'] in [
            BlockType.Text, BlockType.Title,
            BlockType.ImageCaption, BlockType.ImageFootnote,
            BlockType.TableCaption, BlockType.TableFootnote
        ]:
            if len(block['lines']) == 0:
                add_lines_to_block(block)
            elif block['type'] in [BlockType.Title] and len(block['lines']) == 1 and (block['bbox'][3] - block['bbox'][1]) > line_height * 2:
                block['real_lines'] = copy.deepcopy(block['lines'])
                add_lines_to_block(block)
            else:
                for line in block['lines']:
                    bbox = line['bbox']
                    page_line_list.append(bbox)
        elif block['type'] in [BlockType.ImageBody, BlockType.TableBody, BlockType.InterlineEquation]:
            block['real_lines'] = copy.deepcopy(block['lines'])
            add_lines_to_block(block)

    if len(page_line_list) > 200:  # layoutreader最高支持512line
        return None

    # 使用layoutreader排序
    x_scale = 1000.0 / page_w
    y_scale = 1000.0 / page_h
    boxes = []
    # logger.info(f"Scale: {x_scale}, {y_scale}, Boxes len: {len(page_line_list)}")
    for left, top, right, bottom in page_line_list:
        if left < 0:
            logger.warning(
                f'left < 0, left: {left}, right: {right}, top: {top}, bottom: {bottom}, page_w: {page_w}, page_h: {page_h}'
            )  # noqa: E501
            left = 0
        if right > page_w:
            logger.warning(
                f'right > page_w, left: {left}, right: {right}, top: {top}, bottom: {bottom}, page_w: {page_w}, page_h: {page_h}'
            )  # noqa: E501
            right = page_w
        if top < 0:
            logger.warning(
                f'top < 0, left: {left}, right: {right}, top: {top}, bottom: {bottom}, page_w: {page_w}, page_h: {page_h}'
            )  # noqa: E501
            top = 0
        if bottom > page_h:
            logger.warning(
                f'bottom > page_h, left: {left}, right: {right}, top: {top}, bottom: {bottom}, page_w: {page_w}, page_h: {page_h}'
            )  # noqa: E501
            bottom = page_h

        left = round(left * x_scale)
        top = round(top * y_scale)
        right = round(right * x_scale)
        bottom = round(bottom * y_scale)
        assert (
            1000 >= right >= left >= 0 and 1000 >= bottom >= top >= 0
        ), f'Invalid box. right: {right}, left: {left}, bottom: {bottom}, top: {top}'  # noqa: E126, E121
        boxes.append([left, top, right, bottom])
    model_manager = ModelSingleton()
    model = model_manager.get_model('layoutreader')
    with torch.no_grad():
        orders = do_predict(boxes, model)
    sorted_bboxes = [page_line_list[i] for i in orders]

    return sorted_bboxes


def get_line_height(blocks):
    page_line_height_list = []
    for block in blocks:
        if block['type'] in [
            BlockType.Text, BlockType.Title,
            BlockType.ImageCaption, BlockType.ImageFootnote,
            BlockType.TableCaption, BlockType.TableFootnote
        ]:
            for line in block['lines']:
                bbox = line['bbox']
                page_line_height_list.append(int(bbox[3] - bbox[1]))
    if len(page_line_height_list) > 0:
        return statistics.median(page_line_height_list)
    else:
        return 10


def process_groups(groups, body_key, caption_key, footnote_key):
    body_blocks = []
    caption_blocks = []
    footnote_blocks = []
    for i, group in enumerate(groups):
        group[body_key]['group_id'] = i
        body_blocks.append(group[body_key])
        for caption_block in group[caption_key]:
            caption_block['group_id'] = i
            caption_blocks.append(caption_block)
        for footnote_block in group[footnote_key]:
            footnote_block['group_id'] = i
            footnote_blocks.append(footnote_block)
    return body_blocks, caption_blocks, footnote_blocks


def process_block_list(blocks, body_type, block_type):
    indices = [block['index'] for block in blocks]
    median_index = statistics.median(indices)

    body_bbox = next((block['bbox'] for block in blocks if block.get('type') == body_type), [])

    return {
        'type': block_type,
        'bbox': body_bbox,
        'blocks': blocks,
        'index': median_index,
    }


def revert_group_blocks(blocks):
    image_groups = {}
    table_groups = {}
    new_blocks = []
    for block in blocks:
        if block['type'] in [BlockType.ImageBody, BlockType.ImageCaption, BlockType.ImageFootnote]:
            group_id = block['group_id']
            if group_id not in image_groups:
                image_groups[group_id] = []
            image_groups[group_id].append(block)
        elif block['type'] in [BlockType.TableBody, BlockType.TableCaption, BlockType.TableFootnote]:
            group_id = block['group_id']
            if group_id not in table_groups:
                table_groups[group_id] = []
            table_groups[group_id].append(block)
        else:
            new_blocks.append(block)

    for group_id, blocks in image_groups.items():
        new_blocks.append(process_block_list(blocks, BlockType.ImageBody, BlockType.Image))

    for group_id, blocks in table_groups.items():
        new_blocks.append(process_block_list(blocks, BlockType.TableBody, BlockType.Table))

    return new_blocks


def remove_outside_spans(spans, all_bboxes, all_discarded_blocks):
    def get_block_bboxes(blocks, block_type_list):
        return [block[0:4] for block in blocks if block[7] in block_type_list]

    image_bboxes = get_block_bboxes(all_bboxes, [BlockType.ImageBody])
    table_bboxes = get_block_bboxes(all_bboxes, [BlockType.TableBody])
    other_block_type = []
    for block_type in BlockType.__dict__.values():
        if not isinstance(block_type, str):
            continue
        if block_type not in [BlockType.ImageBody, BlockType.TableBody]:
            other_block_type.append(block_type)
    other_block_bboxes = get_block_bboxes(all_bboxes, other_block_type)
    discarded_block_bboxes = get_block_bboxes(all_discarded_blocks, [BlockType.Discarded])

    new_spans = []

    for span in spans:
        span_bbox = span['bbox']
        span_type = span['type']

        if any(calculate_overlap_area_in_bbox1_area_ratio(span_bbox, block_bbox) > 0.4 for block_bbox in
               discarded_block_bboxes):
            new_spans.append(span)
            continue

        if span_type == ContentType.Image:
            if any(calculate_overlap_area_in_bbox1_area_ratio(span_bbox, block_bbox) > 0.5 for block_bbox in
                   image_bboxes):
                new_spans.append(span)
        elif span_type == ContentType.Table:
            if any(calculate_overlap_area_in_bbox1_area_ratio(span_bbox, block_bbox) > 0.5 for block_bbox in
                   table_bboxes):
                new_spans.append(span)
        else:
            if any(calculate_overlap_area_in_bbox1_area_ratio(span_bbox, block_bbox) > 0.5 for block_bbox in
                   other_block_bboxes):
                new_spans.append(span)

    return new_spans

class BookmarkMatchType:
    Auto = 'auto'
    Distance = 'distance'
    EditDist = 'edit_dist'

class BookmarkDistType:
    Auto = 'auto'
    DistXY = 'dist_xy'
    DistY = 'dist_y'

class BookmarkHeaderCorrector:
    def __init__(self, doc):
        bookmark_conf = conf.get_conf()['bookmark_header_corrector']
        self.match_type = bookmark_conf['match_type']
        self.dist_type = bookmark_conf['dist_type']
        self.dist_thresh_max = bookmark_conf['dist_thresh_max']
        self.edit_thresh_max = bookmark_conf['edit_thresh_max']
        self.edit_thresh_max_rate = bookmark_conf['edit_thresh_max_rate']
        self.title_starts = tuple([title_start.lower() for title_start in bookmark_conf['title_starts']])
        self.ignore_starts = bookmark_conf['ignore_starts']
        self.page_id_2_bookmarks = BookmarkHeaderCorrector.get_bookmarks_with_coordinates(doc)

    @staticmethod
    def get_bookmarks_with_coordinates(doc):
        """提取书签及其坐标信息"""
        bookmarks = OrderedDict()
        outline = doc.outline  # 获取大纲（书签）的根节点

        parent_bks = []
        index = 0

        while outline and outline.this.m_internal:
            if index > 10000:
                raise IndexError(f'for loop index {index}, may be infinite loop')

            index += 1
            page_num = outline.page  # 目标页码（0-based）
            if 0 <= page_num < doc.page_count:
                # 获取坐标（处理不同目标类型）
                x, y = outline.x, outline.y

                # # 转换y坐标到PyMuPDF的坐标系
                # page = doc[page_num]
                # height = page.rect.height
                # y_pymu = height - y if y is not None else None

                grade_num = sum([1 if (0 <= bk.page < doc.page_count) else 0 for bk in parent_bks]) + 1
                cur_bk = {
                    'title': outline.title,
                    'page': page_num,
                    'x': x,
                    'y': y,
                    'grade': grade_num
                }

                if page_num in bookmarks:
                    bookmarks[page_num].append(cur_bk)
                else:
                    bookmarks[page_num] = [cur_bk]
                # print(f'index: {index}, cur_bk: {cur_bk}')

            if outline.down:
                parent_bks.append(outline)
                outline = outline.down
                continue
            elif outline.next:
                outline = outline.next
                continue

            while parent_bks:
                outline = parent_bks.pop()
                if outline.next:
                    outline = outline.next
                    break
            else:
                outline = None

        return bookmarks

    @staticmethod
    def replace_anno_in_texts(fix_blocks):
        # python里的注释代码的#号会被当成markdown目录，故先换成//
        for block in fix_blocks:
            if block['type'] != BlockType.Text:
                continue

            lines = block['lines']
            for line in lines:
                spans = line['spans']
                if not spans:
                    continue

                content = spans[0]['content']
                anno_index = 0
                for anno_index, content_c in enumerate(content.lstrip()):
                    if content_c != '#':
                        break
                if anno_index > 0:
                    spans[0]['content'] = '//' + content[anno_index:]

    def get_text_from_block(self, block):
        if block['type'] not in {BlockType.Text, BlockType.Title, BlockType.Discarded}:
            return ''

        result_list = []
        lines = block['lines']
        for line in lines:
            spans = line['spans']
            if not spans:
                continue

            for span in spans:
                result_list.append(span['content'])

        return ''.join(result_list)

    def update_title_block(self, block, grade, bk_title):
        p = re.compile('\s+')
        bk_title = p.sub(' ', bk_title)
        block['type'] = BlockType.Title
        block['lines'] = [block['lines'][0]]
        block['lines'][0]['spans'] = [block['lines'][0]['spans'][0]]
        block['lines'][0]['spans'][0]['content'] = f'|@{grade}@|' + bk_title

    @staticmethod
    def decode_title_grade(md_path, correct_md_path):
        p = re.compile(r'^(# \|@(\d+)@\|).+')
        with open(md_path, 'rt', encoding='utf-8', newline='') as f, open(correct_md_path, 'wt',  encoding='utf-8', newline='') as correct_f:
            for line in f:
                m = p.match(line)
                if m:
                    grade = int(m.group(2))
                    correct_line = '#' * grade + ' ' + line[m.span(1)[1]:]
                else:
                    correct_line = line
                correct_f.write(correct_line)

    def check_block_match_title(self, matching_block, bookmark, edit_thresh_max, edit_thresh_max_rate, is_discarded=False):
        bk_title = bookmark['title']
        bk_title = bk_title.strip().lower()

        block_title = self.get_text_from_block(matching_block).strip().lower()
        if not block_title:
            if not is_discarded:
                LOGGER.error('block_title is empty, bookmark: %s, block_bbox: %s', bookmark, matching_block['bbox'])
            return False

        edit_thresh = min(edit_thresh_max, int(len(bk_title) * edit_thresh_max_rate))
        if bk_title != block_title:
            edit_dist = MinDistance().min_distance(bk_title, block_title)

        if bk_title == block_title or edit_dist <= edit_thresh:
            return True
        else:
            if not is_discarded:
                LOGGER.error('title block not match bookmark, bookmark: %s, block_bbox: %s, edit_dist: %d',
                             bookmark, matching_block['bbox'], edit_dist)
            return False

    def check_block_merge_match_title(self, block_title, neardown_title, bk_title, edit_thresh_max, edit_thresh_max_rate):
        merge_title = (block_title + neardown_title).lower()
        bk_title = bk_title.lower()

        edit_thresh = min(edit_thresh_max, int(len(bk_title) * edit_thresh_max_rate))
        if bk_title != merge_title:
            edit_dist = MinDistance().min_distance(bk_title, merge_title)

        if bk_title == merge_title:
            return True, 0
        else:
            return edit_dist <= edit_thresh, edit_dist

    def allow_title_start(self, block_title, bk_title):
        block_title = block_title.lower()
        bk_title = bk_title.lower()

        if bk_title.startswith(block_title):
            return True

        for title_start in self.title_starts:
            if block_title.startswith(title_start) and bk_title.startswith(title_start):
                return True

        return False

    def find_closest_block(self, matching_blocks, bk_x, bk_y, matched_block_ids):
        min_distance = 999999
        closest_block_index = -1
        for block_index, block in enumerate(matching_blocks):
            if block_index in matched_block_ids:
                continue

            b_lx, b_ly, _, _ = block['bbox']

            if self.dist_type == BookmarkDistType.DistY:
                distance = abs(bk_y - b_ly)
            elif self.dist_type == BookmarkDistType.DistXY:
                distance = math.sqrt((b_lx - bk_x) ** 2 + (b_ly - bk_y) ** 2)
            else:
                raise ValueError('dist_type can only be dist_y/dist_xy, maybe not run judge_dist_type')

            if distance < min_distance:
                closest_block_index = block_index
                min_distance = distance
        return closest_block_index, min_distance

    def find_neardown_block(self, up_block, up_index, matching_blocks, matched_block_ids):
        min_distance = 999999
        closest_block_index = -1
        _, _, _, up_block_rb_y = up_block['bbox']
        for block_index, block in enumerate(matching_blocks):
            if block_index in matched_block_ids or block_index == up_index:
                continue

            b_lx, b_ly, _, _ = block['bbox']
            if b_ly < up_block_rb_y:
                continue

            distance = abs(up_block_rb_y - b_ly)

            if distance < min_distance:
                closest_block_index = block_index
                min_distance = distance
        return closest_block_index, min_distance

    def match_bookmark_with_blocks(self, fix_blocks, fix_discarded_blocks, page_id):
        if not self.page_id_2_bookmarks:
            LOGGER.warning('page_id_2_bookmarks is empty, follback to minerU default bookmark')
            return

        self.judge_match_type()
        self.judge_dist_type()

        LOGGER.info('match_type: %s', self.match_type)
        LOGGER.info('dist_type: %s', self.dist_type)
        if self.match_type == BookmarkMatchType.Distance:
            self.match_bookmark_with_blocks_by_distance(fix_blocks, fix_discarded_blocks, page_id)
        elif self.match_type == BookmarkMatchType.EditDist:
            self.match_bookmark_with_blocks_by_edit_dist(fix_blocks, page_id)
        else:
            raise ValueError(f'illegal match_type: {self.match_type}, maybe some bug')

    def match_bookmark_with_blocks_by_distance(self, fix_blocks, fix_discarded_blocks, page_id):
        LOGGER.info('match_bookmark_with_blocks start, page_id: %d', page_id)

        if page_id not in self.page_id_2_bookmarks:
            for block in fix_blocks:
                if block['type'] == BlockType.Title:
                    block['type'] = BlockType.Text
            LOGGER.info('match_bookmark_with_blocks end, page_id: %d', page_id)
            return

        matching_blocks = fix_blocks + fix_discarded_blocks
        matched_block_ids = set()

        bookmarks = self.page_id_2_bookmarks[page_id]
        for bookmark in bookmarks:
            bk_title, bk_page, bk_x, bk_y, bk_grade = bookmark['title'], bookmark['page'], bookmark['x'], bookmark['y'], bookmark['grade']
            bk_title = bk_title.strip()

            closest_block_index, min_distance = self.find_closest_block(matching_blocks, bk_x, bk_y, matched_block_ids)
            if closest_block_index == -1:
                LOGGER.error('find_closest_block return -1, bookmark: %s', bookmark)
                continue

            matching_block = matching_blocks[closest_block_index]

            if min_distance > self.dist_thresh_max:
                LOGGER.error('bookmark distance with blocks %d greater than dist_thresh_max %d, bookmark: %s, block_bbox: %s',
                             int(min_distance), self.dist_thresh_max, bookmark, matching_block['bbox'])
                continue

            if matching_block['type'] in {BlockType.Title, BlockType.Text}:
                title_match = self.check_block_match_title(matching_block, bookmark, self.edit_thresh_max, self.edit_thresh_max_rate)
                if title_match:
                    matched_block_ids.add(closest_block_index)
                    self.update_title_block(matching_block, bk_grade, bk_title)
                else:
                    LOGGER.error('title_match failed, matching_block: %s, bookmark: %s', matching_block['bbox'], bookmark)
            elif matching_block['type'] == BlockType.Discarded:
                title_match = self.check_block_match_title(matching_block, bookmark, self.edit_thresh_max, self.edit_thresh_max_rate,
                                                      is_discarded=True)
                if title_match:
                    matched_block_ids.add(closest_block_index)
                    self.update_title_block(matching_block, bk_grade, bk_title)
                    continue

                block_title = self.get_text_from_block(matching_block).strip()
                if self.allow_title_start(block_title, bk_title):
                    neardown_block_index, near_distance = (
                        self.find_neardown_block(matching_block, closest_block_index, matching_blocks, matched_block_ids))
                    if neardown_block_index == -1:
                        LOGGER.error('find_neardown_block return -1, matching_block: %s', matching_block['bbox'])
                        continue

                    neardown_block = matching_blocks[neardown_block_index]

                    if near_distance > self.dist_thresh_max:
                        LOGGER.error(
                            'neardown_block distance with matching_block %d greater than dist_thresh_max %d, matching_block: %s, neardown_block: %s',
                            int(min_distance), self.dist_thresh_max, matching_block['bbox'], neardown_block['bbox'])
                        continue

                    neardown_title = self.get_text_from_block(neardown_block).strip()
                    merge_match, merge_edit_dist = self.check_block_merge_match_title(block_title, neardown_title, bk_title,
                                                                                 self.edit_thresh_max, self.edit_thresh_max_rate)
                    if not merge_match:
                        LOGGER.error('merge match failed, merge_edit_dist: %d, block_title: %s, neardown_title: %s, bk_title: %s',
                                     merge_edit_dist, block_title, neardown_title, bk_title)
                        continue

                    # 如果丢弃框加下面的标题框（或文本框）匹配上了书签，侧更新下面的框子，把整个标题更新进去。
                    matched_block_ids.add(closest_block_index)  # 这里仍然是丢弃框，但是把它加进去只是为了不让它再匹配别的书签
                    matched_block_ids.add(neardown_block_index)
                    self.update_title_block(neardown_block, bk_grade, bk_title)
            else:
                LOGGER.error('matching_block type error, type: %s, bbox: %s, bookmark: %s',
                             matching_block['type'], matching_block['bbox'], bookmark)

        for block_index, block in enumerate(fix_blocks):
            if block['type'] == BlockType.Title and block_index not in matched_block_ids:
                block['type'] = BlockType.Text

        for matched_id in sorted(matched_block_ids, reverse=True):
            if matched_id < len(fix_blocks):
                break

            discarded_block_id = matched_id - len(fix_blocks)
            discarded_block = fix_discarded_blocks[discarded_block_id]

            # 如果它的type仍然是原来的discarded，说明它下面的框子才是匹配到书签的标题，所以它本身仍然当丢弃框处理，它的文本内容实际上已经加到下面的框子里了
            if discarded_block['type'] == BlockType.Title:
                fix_blocks.append(discarded_block)
                fix_discarded_blocks.pop(discarded_block_id)

        LOGGER.info('match_bookmark_with_blocks end, page_id: %d', page_id)

    def judge_match_type(self):
        if self.match_type in {BookmarkMatchType.Distance, BookmarkMatchType.EditDist}:
            return

        if self.match_type != BookmarkMatchType.Auto:
            LOGGER.error(f'match_type can only be one of auto/distance/edit_dist, error match_type: {self.match_type}')
            raise ValueError(f'match_type can only be one of auto/distance/edit_dist, error match_type: {self.match_type}')

        bk_y_list = []
        for page_id, bookmarks in self.page_id_2_bookmarks.items():
            bk_y_list_per_page = []
            for bookmark in bookmarks:
                if math.isnan(bookmark['y']):
                    self.match_type = BookmarkMatchType.EditDist
                    return

                bk_y_list_per_page.append(int(bookmark['y']))
                if len(bk_y_list_per_page) >= 3 * len(set(bk_y_list_per_page)):
                    self.match_type = BookmarkMatchType.EditDist
                    return

            bk_y_list.extend(bk_y_list_per_page)

        bk_y_set = set(bk_y_list)

        # 书签y坐标全0，则认为y坐标无效。因为0是页顶，标题不可能在页顶部
        if bk_y_set == {0}:
            self.match_type = BookmarkMatchType.EditDist
            return

        # 页数达到5页以上，y坐标都相同，则认为y坐标无效。
        if len(self.page_id_2_bookmarks) >= 5 and len(bk_y_set) == 1:
            self.match_type = BookmarkMatchType.EditDist
            return

        self.match_type = BookmarkMatchType.Distance
        return

    def judge_dist_type(self):
        if self.dist_type in {BookmarkDistType.DistXY, BookmarkDistType.DistY}:
            return

        if self.dist_type != BookmarkDistType.Auto:
            LOGGER.error(f'dist_type can only be one of auto/dist_xy/dist_y, error dist_type: {self.dist_type}')
            raise ValueError(f'dist_type can only be one of auto/dist_xy/dist_y, error dist_type: {self.dist_type}')

        if self.match_type not in {BookmarkMatchType.Distance, BookmarkMatchType.EditDist}:
            self.judge_match_type()

        if self.match_type == BookmarkMatchType.EditDist:
            return

        if self.match_type != BookmarkMatchType.Distance:
            raise ValueError('match_type != distance, maybe some bug')

        bk_x_list = []
        for page_id, bookmarks in self.page_id_2_bookmarks.items():
            for bookmark in bookmarks:
                if math.isnan(bookmark['x']):
                    self.dist_type = BookmarkDistType.DistY
                    return

                bk_x_list.append(int(bookmark['x']))

        bk_x_set = set(bk_x_list)

        # 书签x坐标全一样，则认为x坐标无效。
        if len(bk_x_set) == 1:
            self.dist_type = BookmarkDistType.DistY
            return

        self.dist_type = BookmarkDistType.DistXY
        return


    def find_block_by_edit_dist(self, matching_blocks, bk_title):
        bk_title = bk_title.strip().lower()
        for block_index, block in enumerate(matching_blocks):
            edit_thresh = min(self.edit_thresh_max, int(len(bk_title) * self.edit_thresh_max_rate))
            block_title = self.get_text_from_block(block).strip().lower()
            if bk_title == block_title:
                return block_index

            edit_dist = MinDistance().min_distance(bk_title, block_title)
            if edit_dist < edit_thresh:
                return block_index

            if self.ignore_starts and bk_title.startswith(self.title_starts):
                for title_start in self.title_starts:
                    if bk_title.startswith(title_start):
                        bk_title = bk_title[len(title_start):]
                        edit_dist = MinDistance().min_distance(bk_title, block_title)
                        if edit_dist < edit_thresh:
                            return block_index

        return -1

    def match_bookmark_with_blocks_by_edit_dist(self, fix_blocks, page_id):
        LOGGER.info('match_bookmark_with_blocks start, page_id: %d', page_id)

        if page_id not in self.page_id_2_bookmarks:
            for block in fix_blocks:
                if block['type'] == BlockType.Title:
                    block['type'] = BlockType.Text
            LOGGER.info('match_bookmark_with_blocks end, page_id: %d', page_id)
            return

        matching_blocks = []
        for block in fix_blocks:
            if block['type'] in {BlockType.Title, BlockType.Text}:
                matching_blocks.append(block)

        # 排个序，从上到下匹配，如果下面的block匹配到书签了，则前面的block不再参与后续的匹配
        matching_blocks.sort(key=lambda b: b['bbox'][1])

        bookmarks = self.page_id_2_bookmarks[page_id]
        for bookmark in bookmarks:
            bk_title, bk_page, bk_grade = bookmark['title'], bookmark['page'], bookmark['grade']
            bk_title = bk_title.strip()

            matched_index = self.find_block_by_edit_dist(matching_blocks, bk_title)
            if matched_index == -1:
                LOGGER.error('bookmark match failed: %s', bookmark)
                continue

            matched_block = matching_blocks[matched_index]
            self.update_title_block(matched_block, bk_grade, bk_title)
            matching_blocks.pop(matched_index)

            for remove_index in range(matched_index - 1, -1, -1):
                remove_block = matching_blocks[remove_index]
                if remove_block['type'] == BlockType.Title:
                    remove_block['type'] = BlockType.Text

                matching_blocks.pop(remove_index)

        for no_match_block in matching_blocks:
            if no_match_block['type'] == BlockType.Title:
                no_match_block['type'] = BlockType.Text

        LOGGER.info('match_bookmark_with_blocks end, page_id: %d', page_id)


def parse_page_core(
    page_doc: PageableData, magic_model, page_id, pdf_bytes_md5, imageWriter, parse_mode, lang,
        bookmark_header_corrector, need_replace_anno_in_texts=False
):
    need_drop = False
    drop_reason = []

    """从magic_model对象中获取后面会用到的区块信息"""
    img_groups = magic_model.get_imgs_v2(page_id)
    table_groups = magic_model.get_tables_v2(page_id)

    """对image和table的区块分组"""
    img_body_blocks, img_caption_blocks, img_footnote_blocks = process_groups(
        img_groups, 'image_body', 'image_caption_list', 'image_footnote_list'
    )

    table_body_blocks, table_caption_blocks, table_footnote_blocks = process_groups(
        table_groups, 'table_body', 'table_caption_list', 'table_footnote_list'
    )

    discarded_blocks = magic_model.get_discarded(page_id)
    text_blocks = magic_model.get_text_blocks(page_id)
    title_blocks = magic_model.get_title_blocks(page_id)
    inline_equations, interline_equations, interline_equation_blocks = magic_model.get_equations(page_id)
    page_w, page_h = magic_model.get_page_size(page_id)

    def merge_title_blocks(blocks, x_distance_threshold=0.1*page_w):
        def merge_two_bbox(b1, b2):
            x_min = min(b1['bbox'][0], b2['bbox'][0])
            y_min = min(b1['bbox'][1], b2['bbox'][1])
            x_max = max(b1['bbox'][2], b2['bbox'][2])
            y_max = max(b1['bbox'][3], b2['bbox'][3])
            return x_min, y_min, x_max, y_max

        def merge_two_blocks(b1, b2):
            # 合并两个标题块的边界框
            b1['bbox'] = merge_two_bbox(b1, b2)

            # 合并两个标题块的文本内容
            line1 = b1['lines'][0]
            line2 = b2['lines'][0]
            line1['bbox'] = merge_two_bbox(line1, line2)
            line1['spans'].extend(line2['spans'])

            return b1, b2

        # 按 y 轴重叠度聚集标题块
        y_overlapping_blocks = []
        title_bs = [b for b in blocks if b['type'] == BlockType.Title]
        while title_bs:
            block1 = title_bs.pop(0)
            current_row = [block1]
            to_remove = []
            for block2 in title_bs:
                if (
                    __is_overlaps_y_exceeds_threshold(block1['bbox'], block2['bbox'], 0.9)
                    and len(block1['lines']) == 1
                    and len(block2['lines']) == 1
                ):
                    current_row.append(block2)
                    to_remove.append(block2)
            for b in to_remove:
                title_bs.remove(b)
            y_overlapping_blocks.append(current_row)

        # 按x轴坐标排序并合并标题块
        to_remove_blocks = []
        for row in y_overlapping_blocks:
            if len(row) == 1:
                continue

            # 按x轴坐标排序
            row.sort(key=lambda x: x['bbox'][0])

            merged_block = row[0]
            for i in range(1, len(row)):
                left_block = merged_block
                right_block = row[i]

                left_height = left_block['bbox'][3] - left_block['bbox'][1]
                right_height = right_block['bbox'][3] - right_block['bbox'][1]

                if (
                    right_block['bbox'][0] - left_block['bbox'][2] < x_distance_threshold
                    and left_height * 0.95 < right_height < left_height * 1.05
                ):
                    merged_block, to_remove_block = merge_two_blocks(merged_block, right_block)
                    to_remove_blocks.append(to_remove_block)
                else:
                    merged_block = right_block

        for b in to_remove_blocks:
            blocks.remove(b)

    """将所有区块的bbox整理到一起"""
    # interline_equation_blocks参数不够准，后面切换到interline_equations上
    interline_equation_blocks = []
    if len(interline_equation_blocks) > 0:
        all_bboxes, all_discarded_blocks = ocr_prepare_bboxes_for_layout_split_v2(
            img_body_blocks, img_caption_blocks, img_footnote_blocks,
            table_body_blocks, table_caption_blocks, table_footnote_blocks,
            discarded_blocks,
            text_blocks,
            title_blocks,
            interline_equation_blocks,
            page_w,
            page_h,
        )
    else:
        all_bboxes, all_discarded_blocks = ocr_prepare_bboxes_for_layout_split_v2(
            img_body_blocks, img_caption_blocks, img_footnote_blocks,
            table_body_blocks, table_caption_blocks, table_footnote_blocks,
            discarded_blocks,
            text_blocks,
            title_blocks,
            interline_equations,
            page_w,
            page_h,
        )

    """获取所有的spans信息"""
    spans = magic_model.get_all_spans(page_id)

    """在删除重复span之前，应该通过image_body和table_body的block过滤一下image和table的span"""
    """顺便删除大水印并保留abandon的span"""
    spans = remove_outside_spans(spans, all_bboxes, all_discarded_blocks)

    """删除重叠spans中置信度较低的那些"""
    spans, dropped_spans_by_confidence = remove_overlaps_low_confidence_spans(spans)
    """删除重叠spans中较小的那些"""
    spans, dropped_spans_by_span_overlap = remove_overlaps_min_spans(spans)

    """根据parse_mode，构造spans，主要是文本类的字符填充"""
    if parse_mode == SupportedPdfParseMethod.TXT:

        """使用新版本的混合ocr方案."""
        spans = txt_spans_extract_v2(page_doc, spans, all_bboxes, all_discarded_blocks, lang)

    elif parse_mode == SupportedPdfParseMethod.OCR:
        pass
    else:
        raise Exception('parse_mode must be txt or ocr')

    """先处理不需要排版的discarded_blocks"""
    discarded_block_with_spans, spans = fill_spans_in_blocks(
        all_discarded_blocks, spans, 0.4
    )
    fix_discarded_blocks = fix_discarded_block(discarded_block_with_spans)

    """如果当前页面没有有效的bbox则跳过"""
    if len(all_bboxes) == 0:
        logger.warning(f'skip this page, not found useful bbox, page_id: {page_id}')
        return ocr_construct_page_component_v2(
            [],
            [],
            page_id,
            page_w,
            page_h,
            [],
            [],
            [],
            interline_equations,
            fix_discarded_blocks,
            need_drop,
            drop_reason,
        )

    """对image和table截图"""
    spans = ocr_cut_image_and_table(
        spans, page_doc, page_id, pdf_bytes_md5, imageWriter
    )

    """span填充进block"""
    block_with_spans, spans = fill_spans_in_blocks(all_bboxes, spans, 0.5)

    """对block进行fix操作"""
    fix_blocks = fix_block_spans_v2(block_with_spans)

    """同一行被断开的titile合并"""
    merge_title_blocks(fix_blocks)

    if bookmark_header_corrector:
        bookmark_header_corrector.match_bookmark_with_blocks(fix_blocks, fix_discarded_blocks, page_id)

    if need_replace_anno_in_texts:
        # python里的注释代码的#号会被当成markdown目录，故先换成//
        BookmarkHeaderCorrector.replace_anno_in_texts(fix_blocks)

    """获取所有line并计算正文line的高度"""
    line_height = get_line_height(fix_blocks)

    """获取所有line并对line排序"""
    sorted_bboxes = sort_lines_by_model(fix_blocks, page_w, page_h, line_height)

    """根据line的中位数算block的序列关系"""
    fix_blocks = cal_block_index(fix_blocks, sorted_bboxes)

    """将image和table的block还原回group形式参与后续流程"""
    fix_blocks = revert_group_blocks(fix_blocks)

    """重排block"""
    sorted_blocks = sorted(fix_blocks, key=lambda b: b['index'])

    """block内重排(img和table的block内多个caption或footnote的排序)"""
    for block in sorted_blocks:
        if block['type'] in [BlockType.Image, BlockType.Table]:
            block['blocks'] = sorted(block['blocks'], key=lambda b: b['index'])

    """获取QA需要外置的list"""
    images, tables, interline_equations = get_qa_need_list_v2(sorted_blocks)

    """构造pdf_info_dict"""
    page_info = ocr_construct_page_component_v2(
        sorted_blocks,
        [],
        page_id,
        page_w,
        page_h,
        [],
        images,
        tables,
        interline_equations,
        fix_discarded_blocks,
        need_drop,
        drop_reason,
    )
    return page_info


def pdf_parse_union(
    model_list,
    dataset: Dataset,
    imageWriter,
    parse_mode,
    start_page_id=0,
    end_page_id=None,
    debug_mode=False,
    lang=None,
):

    pdf_bytes_md5 = compute_md5(dataset.data_bits())

    """初始化空的pdf_info_dict"""
    pdf_info_dict = {}

    """用model_list和docs对象初始化magic_model"""
    magic_model = MagicModel(model_list, dataset)

    """根据输入的起始范围解析pdf"""
    # end_page_id = end_page_id if end_page_id else len(pdf_docs) - 1
    end_page_id = (
        end_page_id
        if end_page_id is not None and end_page_id >= 0
        else len(dataset) - 1
    )

    if end_page_id > len(dataset) - 1:
        logger.warning('end_page_id is out of range, use pdf_docs length')
        end_page_id = len(dataset) - 1

    """初始化启动时间"""
    start_time = time.time()

    doc_convertor_conf = conf.get_conf()['doc_convertor']
    need_replace_anno_in_texts = doc_convertor_conf['replace_anno_in_texts']

    bookmark_header_corrector = None
    if doc_convertor_conf['correct_header_type'] == 'bookmark':
        bookmark_header_corrector = BookmarkHeaderCorrector(dataset._raw_fitz)

    for page_id, page in enumerate(dataset):
        """debug时输出每页解析的耗时."""
        if debug_mode:
            time_now = time.time()
            logger.info(
                f'page_id: {page_id}, last_page_cost_time: {round(time.time() - start_time, 2)}'
            )
            start_time = time_now

        """解析pdf中的每一页"""
        if start_page_id <= page_id <= end_page_id:
            page_info = parse_page_core(
                page, magic_model, page_id, pdf_bytes_md5, imageWriter, parse_mode, lang, bookmark_header_corrector,
                need_replace_anno_in_texts=need_replace_anno_in_texts
            )
        else:
            page_info = page.get_page_info()
            page_w = page_info.w
            page_h = page_info.h
            page_info = ocr_construct_page_component_v2(
                [], [], page_id, page_w, page_h, [], [], [], [], [], True, 'skip page'
            )
        pdf_info_dict[f'page_{page_id}'] = page_info

    """分段"""
    para_split(pdf_info_dict)

    """llm优化"""
    llm_aided_config = get_llm_aided_config()
    if llm_aided_config is not None:
        """公式优化"""
        formula_aided_config = llm_aided_config.get('formula_aided', None)
        if formula_aided_config is not None:
            if formula_aided_config.get('enable', False):
                llm_aided_formula_start_time = time.time()
                llm_aided_formula(pdf_info_dict, formula_aided_config)
                logger.info(f'llm aided formula time: {round(time.time() - llm_aided_formula_start_time, 2)}')
        """文本优化"""
        text_aided_config = llm_aided_config.get('text_aided', None)
        if text_aided_config is not None:
            if text_aided_config.get('enable', False):
                llm_aided_text_start_time = time.time()
                llm_aided_text(pdf_info_dict, text_aided_config)
                logger.info(f'llm aided text time: {round(time.time() - llm_aided_text_start_time, 2)}')
        """标题优化"""
        title_aided_config = llm_aided_config.get('title_aided', None)
        if title_aided_config is not None:
            if title_aided_config.get('enable', False):
                llm_aided_title_start_time = time.time()
                llm_aided_title(pdf_info_dict, title_aided_config)
                logger.info(f'llm aided title time: {round(time.time() - llm_aided_title_start_time, 2)}')

    """dict转list"""
    pdf_info_list = dict_to_list(pdf_info_dict)
    new_pdf_info_dict = {
        'pdf_info': pdf_info_list,
    }

    clean_memory(get_device())

    return new_pdf_info_dict


origin_pdf_parse_union_core_v2.pdf_parse_union = pdf_parse_union