import os

import logging

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import torch
from loguru import logger
from PIL import Image

from magic_pdf.config.constants import MODEL_NAME
from magic_pdf.model.pdf_extract_kit import CustomPEKModel
from magic_pdf.model.sub_modules.model_utils import (
    clean_vram, crop_img, get_res_list_from_layout_res)
from magic_pdf.model.sub_modules.ocr.paddleocr.ocr_utils import (
    get_adjusted_mfdetrec_res, get_ocr_result_list)
import magic_pdf.model.batch_analyze as origin_batch_analyze

from llm_translate.ocr_util import ocr_to_txts
from llm_translate.config import conf
from llm_translate.llm_agent import llm_agent_factory, LLmAgent
from llm_translate.custom_magic_pdf.model.analyze_util import is_contains_code, remove_fomular_from_code
from llm_translate.custom_magic_pdf.model.analyze_util import analyse_local_data


YOLO_LAYOUT_BASE_BATCH_SIZE = 1
MFD_BASE_BATCH_SIZE = 1
MFR_BASE_BATCH_SIZE = 16


LOGGER = logging.getLogger(__name__)

class BatchAnalyze:
    def __init__(self, model: CustomPEKModel, batch_ratio: int):
        self.model = model
        self.batch_ratio = batch_ratio
        analyze_config = conf.get_conf()['batch_analyze']
        self.analyze_agent: LLmAgent = llm_agent_factory.generate(analyze_config['llm_agent_name'])
        self.max_workers = analyze_config['max_workers']
        self.timeout_per_job = analyze_config['timeout_per_job']

    def __call__(self, images: list) -> list:
        images_layout_res = []

        layout_start_time = time.time()
        if self.model.layout_model_name == MODEL_NAME.LAYOUTLMv3:
            # layoutlmv3
            for image in images:
                layout_res = self.model.layout_model(image, ignore_catids=[])
                images_layout_res.append(layout_res)
        elif self.model.layout_model_name == MODEL_NAME.DocLayout_YOLO:
            # doclayout_yolo
            layout_images = []
            modified_images = []
            for image_index, image in enumerate(images):
                pil_img = Image.fromarray(image)
                # width, height = pil_img.size
                # if height > width:
                #     input_res = {'poly': [0, 0, width, 0, width, height, 0, height]}
                #     new_image, useful_list = crop_img(
                #         input_res, pil_img, crop_paste_x=width // 2, crop_paste_y=0
                #     )
                #     layout_images.append(new_image)
                #     modified_images.append([image_index, useful_list])
                # else:
                layout_images.append(pil_img)

            images_layout_res += self.model.layout_model.batch_predict(
                # layout_images, self.batch_ratio * YOLO_LAYOUT_BASE_BATCH_SIZE
                layout_images, YOLO_LAYOUT_BASE_BATCH_SIZE
            )

            layout_images.clear()

            for image_index, useful_list in modified_images:
                for res in images_layout_res[image_index]:
                    for i in range(len(res['poly'])):
                        if i % 2 == 0:
                            res['poly'][i] = (
                                res['poly'][i] - useful_list[0] + useful_list[2]
                            )
                        else:
                            res['poly'][i] = (
                                res['poly'][i] - useful_list[1] + useful_list[3]
                            )
        logger.info(
            f'layout time: {round(time.time() - layout_start_time, 2)}, image num: {len(images)}'
        )

        if self.model.apply_formula:
            # 公式检测
            mfd_start_time = time.time()
            images_mfd_res = self.model.mfd_model.batch_predict(
                # images, self.batch_ratio * MFD_BASE_BATCH_SIZE
                images, MFD_BASE_BATCH_SIZE
            )
            logger.info(
                f'mfd time: {round(time.time() - mfd_start_time, 2)}, image num: {len(images)}'
            )

            # 公式识别
            mfr_start_time = time.time()
            images_formula_list = self.model.mfr_model.batch_predict(
                images_mfd_res,
                images,
                batch_size=self.batch_ratio * MFR_BASE_BATCH_SIZE,
            )
            mfr_count = 0
            for image_index in range(len(images)):
                images_layout_res[image_index] += images_formula_list[image_index]
                mfr_count += len(images_formula_list[image_index])
            logger.info(
                f'mfr time: {round(time.time() - mfr_start_time, 2)}, image num: {mfr_count}'
            )

        # 清理显存
        clean_vram(self.model.device, vram_threshold=8)

        # reference: magic_pdf/model/doc_analyze_by_custom_model.py:doc_analyze
        output_dir = Path(analyse_local_data.output_dir) / 'failed_ocr_res'
        output_dir.mkdir(parents=True, exist_ok=True)

        executor = ThreadPoolExecutor(self.max_workers)
        future_num = 0
        indexs_to_params = {}
        page_params = []
        for index in range(len(images)):
            layout_res = images_layout_res[index]
            pil_img = Image.fromarray(images[index])

            ocr_res_list, table_res_list, single_page_mfdetrec_res = (
                get_res_list_from_layout_res(layout_res)
            )

            page_params.append((ocr_res_list, table_res_list))

            # Process each area that requires OCR processing
            for res_index, res in enumerate(ocr_res_list):
                new_image, useful_list = crop_img(
                    res, pil_img, crop_paste_x=50, crop_paste_y=50
                )
                adjusted_mfdetrec_res = get_adjusted_mfdetrec_res(
                    single_page_mfdetrec_res, useful_list
                )

                # OCR recognition
                new_image = cv2.cvtColor(np.asarray(new_image), cv2.COLOR_RGB2BGR)

                future = None
                if self.model.apply_formula and res['category_id'] == 1 and adjusted_mfdetrec_res:
                    res_txts = ocr_to_txts(new_image)

                    if not res_txts:
                        LOGGER.error('res_txts is empty, index: %d, res_index: %d', index, res_index)
                        cv2.imwrite(str(output_dir / f'{index:04d}_{res_index:04d}.jpg'), new_image)

                    future = executor.submit(is_contains_code, self.analyze_agent, res_txts)
                    future_num += 1

                indexs_to_params[(index, res_index)] = (future, adjusted_mfdetrec_res, useful_list, new_image)

        future_index = 0
        ocr_time = 0
        ocr_count = 0
        table_time = 0
        table_count = 0
        for index in range(len(images)):
            layout_res = images_layout_res[index]
            ocr_res_list, table_res_list = page_params[index]
            for res_index, res in enumerate(ocr_res_list):
                future, adjusted_mfdetrec_res, useful_list, new_image = indexs_to_params[(index, res_index)]

                try:
                    contains_code = future.result() if future else False
                    if contains_code:
                        remove_fomular_from_code(useful_list, adjusted_mfdetrec_res, layout_res)
                except:
                    LOGGER.exception('is_contains_code occur exception, index: %d, res_index: %d', index, res_index)

                if future:
                    future_index += 1
                    LOGGER.info('is_contains_code, finish: %d, total: %d', future_index, future_num)

                # ocr识别
                ocr_start = time.time()
                if self.model.apply_ocr:
                    ocr_res = self.model.ocr_model.ocr(
                        new_image, mfd_res=adjusted_mfdetrec_res
                    )[0]
                else:
                    ocr_res = self.model.ocr_model.ocr(
                        new_image, mfd_res=adjusted_mfdetrec_res, rec=False
                    )[0]

                # Integration results
                if ocr_res:
                    ocr_result_list = get_ocr_result_list(ocr_res, useful_list)
                    layout_res.extend(ocr_result_list)
            ocr_time += time.time() - ocr_start
            ocr_count += len(ocr_res_list)

            # 表格识别 table recognition
            if self.model.apply_table:
                table_start = time.time()
                for res in table_res_list:
                    new_image, _ = crop_img(res, pil_img)
                    single_table_start_time = time.time()
                    html_code = None
                    if self.model.table_model_name == MODEL_NAME.STRUCT_EQTABLE:
                        with torch.no_grad():
                            table_result = self.model.table_model.predict(
                                new_image, 'html'
                            )
                            if len(table_result) > 0:
                                html_code = table_result[0]
                    elif self.model.table_model_name == MODEL_NAME.TABLE_MASTER:
                        html_code = self.model.table_model.img2html(new_image)
                    elif self.model.table_model_name == MODEL_NAME.RAPID_TABLE:
                        html_code, table_cell_bboxes, logic_points, elapse = (
                            self.model.table_model.predict(new_image)
                        )
                    run_time = time.time() - single_table_start_time
                    if run_time > self.model.table_max_time:
                        logger.warning(
                            f'table recognition processing exceeds max time {self.model.table_max_time}s'
                        )
                    # 判断是否返回正常
                    if html_code:
                        expected_ending = html_code.strip().endswith(
                            '</html>'
                        ) or html_code.strip().endswith('</table>')
                        if expected_ending:
                            res['html'] = html_code
                        else:
                            logger.warning(
                                'table recognition processing fails, not found expected HTML table end'
                            )
                    else:
                        logger.warning(
                            'table recognition processing fails, not get html return'
                        )
                table_time += time.time() - table_start
                table_count += len(table_res_list)
        executor.shutdown()

        if self.model.apply_ocr:
            logger.info(f'ocr time: {round(ocr_time, 2)}, image num: {ocr_count}')
        else:
            logger.info(f'det time: {round(ocr_time, 2)}, image num: {ocr_count}')
        if self.model.apply_table:
            logger.info(f'table time: {round(table_time, 2)}, image num: {table_count}')

        return images_layout_res


    # def is_contains_code(self, res_txts):
    #     prompt = ("下面这段文本是在pdf中提取出来，转为markdown格式，请帮我分类一下，分类条目为：1.包含代码，2.不包含代码。注意：a.数学公式不算代码。"
    #               "b.文本格式可能有点乱，不需要是完全正确、可运行的代码，只要包含了比如python、c++之类的代码语句，就算是包含代码。"
    #               "直接给出分类条目的编号，不要给任何解释：\n{content}")
    #
    #     llm_result: str = self.analyze_agent.ask_llm(prompt, res_txts)
    #     llm_result = llm_result.split('</think>')[-1].replace(' ', '')
    #     contains_code = llm_result in ['1', '1.包含代码', '1包含代码']
    #     return contains_code
    #
    # def remove_fomular_from_code(self, useful_list, adjusted_mfdetrec_res, layout_res):
    #     paste_x, paste_y, xmin, ymin, xmax, ymax, new_width, new_height = useful_list
    #
    #     for mfd_res in adjusted_mfdetrec_res:
    #         x0, y0, x1, y1 = mfd_res['bbox']
    #         mf_xmin = x0 + xmin - paste_x
    #         mf_ymin = y0 + ymin - paste_y
    #         mf_xmax = x1 + xmin - paste_x
    #         mf_ymax = y1 + ymin - paste_y
    #
    #         layout_res_index = 0
    #         while layout_res_index < len(layout_res):
    #             per_layout_res = layout_res[layout_res_index]
    #             res_poly = per_layout_res['poly']
    #             res_type = per_layout_res['category_id']
    #             if res_type not in [13, 14]:
    #                 layout_res_index += 1
    #                 continue
    #
    #             if mf_xmin == res_poly[0] and mf_ymin == res_poly[1] and mf_xmax == res_poly[4] and mf_ymax == res_poly[5]:
    #                 layout_res.pop(layout_res_index)
    #             else:
    #                 layout_res_index += 1
    #     adjusted_mfdetrec_res.clear()

origin_batch_analyze.BatchAnalyze = BatchAnalyze