import logging
import os

from llm_translate.doc_convert import DocConvertor
from llm_translate.llm_trans import LlmTranslator


LOGGER = logging.getLogger(__name__)


class Translator:
    def __init__(self, output_dir='output'):
        self.doc_convertor = DocConvertor(output_dir)
        self.llm_translator = LlmTranslator()

    def translate(self, pdf_path, only_pdf2md=False, only_md=False, start_page=1, end_page=None):
        md_path = self.doc_convertor.pdf2md(pdf_path, start_page=start_page, end_page=end_page)

        if only_pdf2md:
            LOGGER.info('only_pdf2md is %s, only do pdf2md, md_path: %s', only_pdf2md, md_path)
            return md_path

        md_trans_path = self.llm_translator.do_translate(md_path)

        if only_md:
            LOGGER.info('only_md is %s, only do translate, md_trans_path: %s', only_md, md_trans_path)
            return md_trans_path

        result_pdf_path = self.doc_convertor.md2pdf(md_trans_path)
        LOGGER.info('finish all step, result_pdf_path: %s', result_pdf_path)
        return result_pdf_path