import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from llm_translate.llm_agent import llm_agent_factory
from llm_translate.split_utils import split_md
from llm_translate.config import conf
from llm_translate.corrector.code_format import CodeFomatter
from llm_translate.corrector.imagepath_correct import ImagePathCorrector


LOGGER = logging.getLogger(__name__)


class LlmTranslator:
    def __init__(self):
        self.code_formatter = CodeFomatter()
        self.imagepath_corrector = ImagePathCorrector()

        trans_conf = conf.get_conf()['llm_translator']
        self.chunk_size = trans_conf['chunk_size']
        self.title_add_size = trans_conf['title_add_size']
        self.need_format_code = trans_conf['need_format_code']
        self.timeout = trans_conf['timeout']
        self.max_workers = trans_conf['max_workers']
        self.need_correct_imagepath = trans_conf['need_correct_imagepath']

        if isinstance(trans_conf['llm_agent_name'], list):
            self.llm_agents = []
            for llm_agent_name in trans_conf['llm_agent_name']:
                self.llm_agents.append(llm_agent_factory.generate(llm_agent_name))
        elif isinstance(trans_conf['llm_agent_name'], str):
            self.llm_agents = [llm_agent_factory.generate(trans_conf['llm_agent_name'])]
        else:
            raise ValueError('llm_translator.llm_agent_name only can be a list or a string')

        self.lock = Lock()
        self.cur_agent_index = -1
        # if hasattr(self.llm_agent, 'tpm') and self.llm_agent.tpm > 0:
        #     self.max_workers = max(min(self.llm_agent.tpm // self.chunk_size, self.max_workers), 1)

    def translate_by_llm(self, content):
        prompt = ("这是一个markdown文件中的一段，把它翻译成中文，不要修改任何markdown标记，不要使用code wrapper (```)，"
                  "这个片段中可能会插入图片链接及图片标题、html表格及表格标题，导致某段文字内容被分隔开，翻译的时候尽量不要丢失内容。"
                  "这个片段中可能会插入代码，但是代码可能丢失了缩进空格，帮我补充一下缩进，并且在代码开头和结尾加上对应的code wrapper(比如开头加上```cpp，结尾加上```，注意只包代码片段，不要把多余的内容包进来)。"
                  "除上述要求之外，翻译原文即可，不要在末尾接龙。"
                  "直接返回翻译结果，不要给任何解释或描述：\n{content}")

        space_chars = {' ', '\r', '\n', '\t'}
        start_spaces = ''
        end_spaces = ''

        for c in content:
            if c in space_chars:
                start_spaces += c
            else:
                break

        for index in range(len(content) - 1, len(start_spaces), -1):
            c = content[index]
            if c in space_chars:
                end_spaces = c + end_spaces
            else:
                break

        trans_result = None
        try:
            self.lock.acquire_lock()
            self.cur_agent_index += 1
            self.cur_agent_index %= len(self.llm_agents)
            self.lock.release_lock()
            LOGGER.info('using agent index: %d', self.cur_agent_index)
            trans_result = self.llm_agents[self.cur_agent_index].ask_llm(prompt, content)

            if trans_result and self.need_correct_imagepath:
                trans_result = self.imagepath_corrector.correct_imagepath(content, trans_result)

        except:
            LOGGER.exception("translate occur exeption, cotent: %s", content)
        else:
            LOGGER.info("translate return, cotent: %s\ntrans_result: %s", content, trans_result)

        if trans_result:
            trans_result = start_spaces + trans_result.strip() + end_spaces

        return trans_result

    def do_translate(self, md_path):
        with open(md_path, 'rt', encoding='utf-8', newline='') as f:
            md_txt = f.read()
            LOGGER.info('md_txt len: %d', len(md_txt))

            split_results = split_md(md_txt, chunk_size=self.chunk_size, title_add_size=self.title_add_size)

        trans_results = [""] * len(split_results)

        if self.max_workers > 0:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_list = []
                for index, section in enumerate(split_results):
                    LOGGER.info('%d: %s', index, section)

                    future_list.append(executor.submit(self.translate_by_llm, section))

                for trans_index, future in enumerate(future_list):
                    trans_result = future.result(timeout=self.timeout)
                    if trans_result is None:
                        trans_result = split_results[trans_index]

                    trans_results[trans_index] = trans_result
        else:
            for index, section in enumerate(split_results):
                LOGGER.info('%d: %s', index, section)

                trans_result =  self.translate_by_llm(section)
                if trans_result is None:
                    trans_result = section
                trans_results[index] = trans_result

        md_trans_txt = '\n'.join(trans_results)
        md_path = Path(md_path)
        md_trans_path = md_path.parent / (md_path.stem + "_trans.md")
        with open(md_trans_path, 'wt', encoding='utf-8', newline='') as f:
            f.write(md_trans_txt)

        if self.need_format_code:
            format_md_trans_path = md_path.parent / (md_path.stem + "_trans_format.md")
            self.code_formatter.do_correct(md_trans_path, format_md_trans_path)
            return format_md_trans_path

        return md_trans_path


if __name__ == '__main__':
    with open(r'D:\workPython\llm\llm_translate\output\test\text.txt', 'rt', encoding='utf-8', newline='') as f:
        content = f.read()

    result = LlmTranslator().translate_by_llm(content)

    with open(r'D:\workPython\llm\llm_translate\output\test\text_result.txt', 'wt', encoding='utf-8', newline='') as f:
        f.write(result)