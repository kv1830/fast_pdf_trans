import logging
import logging.config
import sys
import os
import argparse
from pathlib import Path

FORMAT = '%(asctime)-s %(name)s %(funcName)s %(lineno)d %(levelname)-8s %(message)s'
logging.basicConfig(format=FORMAT, stream=sys.stderr, level=logging.INFO)

from llm_translate.config import conf


LOGGING_NAME = "llm_translate"
LLM_LOGGING_NAME = "llm_translate.llm_agent"


def set_logging(verbose=True):
    os.makedirs('log', exist_ok=True)

    # sets up logging for the given name
    level = logging.DEBUG if verbose else logging.INFO

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            f"{LOGGING_NAME}_fmt": {
                "format": "%(asctime)s %(name)s %(funcName)s %(lineno)d %(levelname)-8s %(message)s"}},
        "handlers": {
            f"{LOGGING_NAME}_rotate_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": f"{LOGGING_NAME}_fmt",
                "level": level,
                "encoding": "utf-8",
                "filename": "log/translate.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 10},
            f"{LLM_LOGGING_NAME}_rotate_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": f"{LOGGING_NAME}_fmt",
                "level": level,
                "encoding": "utf-8",
                "filename": "log/llm.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 10},
            f"{LOGGING_NAME}_console": {
                "class": "logging.StreamHandler",
                "formatter": f"{LOGGING_NAME}_fmt",
                "level": level}},
        "loggers": {
            LOGGING_NAME: {
                "level": level,
                "handlers": [f"{LOGGING_NAME}_rotate_file", f"{LOGGING_NAME}_console"],
                "propagate": False,},
            LLM_LOGGING_NAME: {
                "level": level,
                "handlers": [f"{LLM_LOGGING_NAME}_rotate_file", f"{LOGGING_NAME}_console"],
                "propagate": False, }}})  # 这里如果不指定为False，则默认为True，则此logger写日志时，yolov5.log中会同时写两条，有一条是basicConfig中的，即root的


set_logging()  # run before defining LOGGER


LOGGER = logging.getLogger(LOGGING_NAME)


def parse_args():
    root_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()

    parser.add_argument("pdf_path", help="pdf原文件路径")
    parser.add_argument("--output-path", default=root_dir/'output', help="结果文件目录")
    parser.add_argument("--only-pdf2md", action="store_true", help="仅将pdf转成markdown，不做翻译")
    parser.add_argument("--only-md", action="store_true", help="仅输出markdown格式的翻译结果，不转成pdf")
    parser.add_argument("--start-page", type=int, default=1, help="pdf起始页号，从1开始算，含本页。默认1。")
    parser.add_argument("--end-page", type=int, default=None, help="pdf结束页号，从1开始算，含本页。默认None，表示无结束页。")

    # 如果开启此项，则下面的命令行参数会覆盖conf.yaml中的
    parser.add_argument("--override-conf", action="store_true", help="如果开启此项，则下面的命令行参数会覆盖conf.yaml中的")
    parser.add_argument("--force-ocr", action="store_true",
                        help="开启则在解析pdf中的文档内容时强制使用ocr进行文字识别，文字版的pdf多数不需要开启（除非格式特殊，解析不出文字，则可以开启）。")
    parser.add_argument("--table-enable", action="store_true",
                        help="透传MinerU的参数，开启则会解析表格中的内容，但会较大降低转换速度，并且有可能导致结果pdf渲染失败。不开启则直接将表格截图，如非必要，不建议开启，")
    parser.add_argument("--formula-enable", action="store_true",
                        help="透传MinerU的参数，开启则进行公式识别并转换成Latex格式，但会较大降低转换速度，并且有可能把非公式字符解析成特殊格式。除非确定pdf中有公式，否则不要开启。")
    parser.add_argument("--correct-header-type", default="bookmark", choices=['no','bookmark', 'by_llm', 'by_llm_easy'],
                        help="MinerU将pdf转成markdown时，都是一级标题，并且可能有多余的标题，所以需要进行修正，"
                             "可选值为：no/bookmark/by_llm/by_llm_easy no:不修正，bookmark:通过pdf书签修正，by_llm:通过大模型修正，"
                             "by_llm_easy:通过大模型修正，但只保留1、2级标题，有书签则选bookmark，否则建议选by_llm_easy。默认bookmark")

    parser.add_argument("--remove-error-formula", action="store_true",
                        help="魔改MinerU的功能，暂时只排除代码块中的公式。因为代码中不可能有Latex公式，并且代码段中的字符经常被公式格式。只有在formula_enable开启时才有效。")
    parser.add_argument("--replace-anno-in-texts", action="store_true",
                        help="魔改MinerU的功能，文本块的python代码的#号注释，在markdown里会被当成标题，所以转成c++的注释符号，暂时规避")
    parser.add_argument("--single-mode", action="store_true",
                        help="解析pdf时，显存大于8G默认走batch-mode，但是页数多的时候有可能会占用更多内存，开启此项后强制不走batch-mode")
    parser.add_argument("--override-history", action="store_true",
                        help="解析pdf的流程为pdf->markdown->修正标题的markdown，后面为翻译流程。如果上次任务执行失败，可以再次执行。"
                             "已经完成的解析pdf步骤不会再重新执行。开启此项之后会删除输出目录下的所有文件，重新执行整个任务。")
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = parse_args()
    if args.override_conf:
        conf.override_conf(args.__dict__)

    # 放这里的话，就不至于命令行参数输错了还要等它加载完模型才知道了
    from llm_translate.translator import Translator
    tranlator = Translator(output_dir=str(args.output_path))
    tranlator.translate(args.pdf_path, only_pdf2md=args.only_pdf2md, only_md=args.only_md,
                        start_page=args.start_page, end_page=args.end_page)

