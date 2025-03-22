import os
from pathlib import Path
import shutil
import logging
import subprocess
import platform
import yaml

from llm_translate.config import conf

class CorrectHeaderType:
    NO = 'no'
    Bookmark = 'bookmark'
    ByLlm = 'by_llm'
    ByLlmEasy = 'by_llm_easy'

convert_conf = conf.get_conf()['doc_convertor']
# 如果不使用书签修正标题（比如就没有书签），但是开启了replace_anno_in_texts，则仍然需要导入魔改代码
if convert_conf['correct_header_type'] == CorrectHeaderType.Bookmark or convert_conf['replace_anno_in_texts']:
    import llm_translate.custom_magic_pdf.pre_proc.ocr_dict_merge
    import llm_translate.custom_magic_pdf.pdf_parse_union_core_v2


if convert_conf['formula_enable'] and convert_conf['remove_error_formula']:
    import llm_translate.custom_magic_pdf.model.pdf_extract_kit
    from llm_translate.custom_magic_pdf.model.pdf_extract_kit import analyse_local_data
    import llm_translate.custom_magic_pdf.model.batch_analyze
else:
    analyse_local_data = None

from magic_pdf.data.data_reader_writer import FileBasedDataWriter, FileBasedDataReader
from magic_pdf.data.dataset import PymuDocDataset
from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze
from magic_pdf.config.enums import SupportedPdfParseMethod

import pypandoc

from llm_translate.corrector.header_factory import HeaderFactory

LOGGER = logging.getLogger(__name__)


class DocConvertor:
    def __init__(self, output_root="output"):
        self.output_root = output_root
        convert_conf = conf.get_conf()['doc_convertor']

        self.correct_header_type = convert_conf['correct_header_type']
        if self.correct_header_type not in {CorrectHeaderType.NO, CorrectHeaderType.Bookmark, CorrectHeaderType.ByLlm,
                                            CorrectHeaderType.ByLlmEasy}:
            raise ValueError('correct_header_type can only be no/bookmark/by_llm/by_llm_easy')

        if self.correct_header_type in {CorrectHeaderType.ByLlm, CorrectHeaderType.ByLlmEasy}:
            easy_header = self.correct_header_type == CorrectHeaderType.ByLlmEasy
            self.header_correcter = HeaderFactory(easy_header).generate()

        self.force_ocr = convert_conf['force_ocr']
        self.table_enable = convert_conf['table_enable']
        self.formula_enable = convert_conf['formula_enable']

        self.single_mode = convert_conf['single_mode']
        self.override_history = convert_conf['override_history']
        self.font = convert_conf['font']
        self.sure_has_font = convert_conf['sure_has_font']

        dict_yaml_path = (Path(__file__).parent.parent / "dict" / "font_dict.yaml").resolve()

        with open(dict_yaml_path, 'rt', encoding='utf-8') as f:
            self.font_dict = yaml.safe_load(f)

    def convert_to_md(self, output_dir, pdf_file_name, name_without_suff, start_page_id=0, end_page_id=None):
        # prepare env
        local_image_dir, local_md_dir = str(output_dir / "images"), str(output_dir)
        image_dir = str(os.path.basename(local_image_dir))

        os.makedirs(local_image_dir, exist_ok=True)

        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(
            local_md_dir
        )

        # read bytes
        reader1 = FileBasedDataReader("")
        pdf_bytes = reader1.read(pdf_file_name)  # read the pdf content

        # proc
        ## Create Dataset Instance
        ds = PymuDocDataset(pdf_bytes)

        ## inference
        if ds.classify() == SupportedPdfParseMethod.OCR or self.force_ocr:
            infer_result = ds.apply(doc_analyze, ocr=True, start_page_id=start_page_id, end_page_id=end_page_id,
                                    table_enable=self.table_enable, formula_enable=self.formula_enable)

            ## pipeline
            pipe_result = infer_result.pipe_ocr_mode(image_writer)

        else:
            infer_result = ds.apply(doc_analyze, ocr=False, start_page_id=start_page_id, end_page_id=end_page_id,
                                    table_enable=self.table_enable, formula_enable=self.formula_enable)

            ## pipeline
            pipe_result = infer_result.pipe_txt_mode(image_writer)

        ### draw model result on each page
        infer_result.draw_model(os.path.join(local_md_dir, f"{name_without_suff}_model.pdf"))

        ### get model inference result
        model_inference_result = infer_result.get_infer_res()

        ### draw layout result on each page
        pipe_result.draw_layout(os.path.join(local_md_dir, f"{name_without_suff}_layout.pdf"))

        ### draw spans result on each page
        pipe_result.draw_span(os.path.join(local_md_dir, f"{name_without_suff}_spans.pdf"))

        # ### get markdown content
        # md_content = pipe_result.get_markdown(image_dir)

        ### dump markdown
        md_filename = f"{name_without_suff}.md"
        pipe_result.dump_md(md_writer, md_filename, image_dir)

        # ### get content list content
        # content_list_content = pipe_result.get_content_list(image_dir)

        ### dump content list
        pipe_result.dump_content_list(md_writer, f"{name_without_suff}_content_list.json", image_dir)

        # ### get middle json
        # middle_json_content = pipe_result.get_middle_json()

        ### dump middle json
        pipe_result.dump_middle_json(md_writer, f'{name_without_suff}_middle.json')
        return md_filename

    def pdf2md(self, pdf_path, start_page=1, end_page=None):
        if self.single_mode:
            # 解析pdf时，显存大于8G默认走batch-mode，但是页数多的时候有可能会占用更多内存。MinerU判断显存的时候会先读这个环境变量
            os.environ['VIRTUAL_VRAM_SIZE'] = '6'

        pdf_file_name = Path(pdf_path)  # replace with the real pdf path
        name_without_suff = pdf_file_name.stem

        output_dir = Path(self.output_root) / name_without_suff
        if self.override_history and output_dir.exists():
            shutil.rmtree(output_dir)

        output_dir.mkdir(exist_ok=True, parents=True)

        # 给魔改的batch_analyze.py用
        os.environ['doc_convert_output_dir'] = str(output_dir)

        if analyse_local_data:
            analyse_local_data.page_index = 0
            analyse_local_data.output_dir = str(output_dir)

        md_filename = f"{name_without_suff}.md"
        if not (output_dir / md_filename).exists():
            start_page_id = start_page - 1
            end_page_id = end_page - 1 if end_page is not None else None
            self.convert_to_md(output_dir, pdf_file_name, name_without_suff, start_page_id=start_page_id,  end_page_id=end_page_id)

        md_path = output_dir / md_filename
        if self.correct_header_type == CorrectHeaderType.NO:
            return str(md_path)
        elif self.correct_header_type == CorrectHeaderType.Bookmark:
            from llm_translate.custom_magic_pdf.pdf_parse_union_core_v2 import BookmarkHeaderCorrector
            correct_md_path = output_dir / (md_path.stem + "_correct.md")
            BookmarkHeaderCorrector.decode_title_grade(md_path, correct_md_path)
            return correct_md_path
        elif self.correct_header_type in {CorrectHeaderType.ByLlm, CorrectHeaderType.ByLlmEasy}:
            correct_md_path = output_dir / (md_path.stem + "_correct.md")

            if not correct_md_path.exists():
                correct_flag = self.correct_headers(md_path, correct_md_path)
                if not correct_flag:
                    return md_path

            return correct_md_path
        else:
            raise ValueError('correct_header_type can only be no/bookmark/by_llm/by_llm_easy')

    def correct_headers(self, md_path, correct_md_path, encoding='utf-8'):
        return self.header_correcter.do_correct(md_path, correct_md_path, encoding=encoding)

    def md2pdf(self, md_path):
        LOGGER.info('md2pdf, md_path: %s', md_path)

        self.copy_ttf()

        md_path = Path(md_path).absolute()
        cwd = os.getcwd()
        os.chdir(md_path.parent)
        pdf_path = md_path.parent / (md_path.stem + ".pdf")

        if platform.system() != 'Linux':
            pypandoc.convert_file(str(md_path), 'pdf', format='markdown+tex_math_dollars',
                                  extra_args=['--pdf-engine=prince',
                                              '-V', 'mainfont=STSong,华文宋体',
                                              '-V', 'CJKmainfont=STSong,华文宋体',
                                              ],
                                  encoding='utf-8',
                                  outputfile=str(pdf_path))
        else:
            self.write_css(md_path)
            pypandoc.convert_file(str(md_path), 'pdf', format='markdown+tex_math_dollars',
                                  extra_args=['--pdf-engine=prince',
                                              '-V', 'mainfont=STSong,华文宋体',
                                              '-V', 'CJKmainfont=STSong,华文宋体',
                                              '--css=style.css',
                                              ],
                                  encoding='utf-8',
                                  outputfile=str(pdf_path))

        os.chdir(cwd)
        return str(pdf_path)

    def write_css(self, md_path):
        md_path = Path(md_path)
        css_path = md_path.parent / 'style.css'
        with open(css_path, 'wt', encoding='utf-8') as f:
            f.write('* { font-family: ' + self.font + ', prince-no-fallback; }')

    def copy_ttf(self):
        if platform.system() != 'Linux' or self.sure_has_font:
            return

        dest_ttf_dir = Path(os.path.expanduser('~')) / '.fonts'
        dest_ttf_dir.mkdir(exist_ok=True, parents=True)

        copy_new_file = False
        font_files = self.font_dict[self.font]
        for font_file in font_files:
            dest_ttf_path = dest_ttf_dir / font_file
            if dest_ttf_path.exists():
                continue

            dest_ttf_path.parent.mkdir(exist_ok=True, parents=True)

            src_ttf_path = Path(__file__).parent.parent / 'ttf' / font_file
            shutil.copy(src_ttf_path, dest_ttf_path)
            copy_new_file = True

        if copy_new_file:
            result = subprocess.run(['fc-cache', '-fv'], capture_output=True, text=True, encoding='utf-8')
            LOGGER.error('fc-cache -fv stderr: %s', result.stderr)
            LOGGER.error('fc-cache -fv stdout: %s', result.stdout)


if __name__ == '__main__':
    # md_path = r'D:\workPython\llm\fast_pdf_trans\output\Hands-On Generative AI with Transformers and Diffusion Models\Hands-On Generative AI with Transformers and Diffusion Models_correct_trans_format.md'
    md_path = r'D:\workPython\llm\fast_pdf_trans\README_zh-CN.md'
    convertor = DocConvertor()
    convertor.md2pdf(md_path)