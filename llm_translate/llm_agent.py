import logging

from pathlib import Path

from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain.chains import LLMChain

from llm_translate.config import conf
from llm_translate.llm_rate_control import RateControl
from llm_translate.llm_cache import LLmCache


LOGGER = logging.getLogger(__name__)


class LLmAgent:
    def __init__(self, base_url, model_name, api_key, timeout=60, max_retries=2, use_cache=False, cache_file_name=None,
                 streaming=False):
        self.base_url = base_url
        self.model_name = model_name
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.use_cache = use_cache
        self.cache_file_name = cache_file_name
        self.streaming = streaming
        if use_cache:
            self.llmcache = LLmCache(Path(f'./cache/{cache_file_name}.txt'))

    def format_prompt(self, prompt, prompt_var):
        prompt = PromptTemplate.from_template(prompt)
        if isinstance(prompt_var, str):
            prompt_str = prompt.format(**{prompt.input_variables[0]: prompt_var})
        else:
            prompt_str = prompt.format(**prompt_var)
        return prompt_str

    def ask_llm_by_api(self, prompt, prompt_var):
        prompt = PromptTemplate.from_template(prompt)

        # llm = OpenAI(base_url=base_url, model_name=model_name, api_key=api_key)
        llm = ChatOpenAI(base_url=self.base_url, model_name=self.model_name, api_key=self.api_key, timeout=self.timeout,
                         max_retries=self.max_retries, temperature=0, streaming=self.streaming)
        chain = LLMChain(
            llm=llm,
            prompt=prompt
        )

        if isinstance(prompt_var, str):
            output = chain(prompt_var)
            text = output['text']
        else:
            text = chain.run(**prompt_var)
        return text

    def in_cache(self, prompt, prompt_var):
        if not self.use_cache:
            return False

        prompt_str = self.format_prompt(prompt, prompt_var)
        return prompt_str in self.llmcache

    def ask_llm(self, prompt, prompt_var):
        LOGGER.debug('prompt: %s, prompt_val: %s', prompt, str(prompt_var)[:100])
        prompt_str = self.format_prompt(prompt, prompt_var)
        LOGGER.debug('prompt_str: %s', prompt_str)

        if self.use_cache:
            text = self.llmcache.get(prompt_str)
            if text is None:
                text = self.ask_llm_by_api(prompt, prompt_var)
                self.llmcache.save_one(prompt_str, text)
        else:
            text = self.ask_llm_by_api(prompt, prompt_var)

        LOGGER.debug('text: %s', text)

        return text

class LLmAgentFactory:
    def __init__(self):
        self.agent_dict = {}

    def generate(self, llm_agent_name):
        if llm_agent_name in self.agent_dict:
            return self.agent_dict[llm_agent_name]

        llm_agent_conf = conf.get_conf()[llm_agent_name]
        base_url = llm_agent_conf['base_url']
        model_name = llm_agent_conf['model_name']
        api_key = llm_agent_conf['api_key']
        timeout = llm_agent_conf['timeout']

        rate_control = llm_agent_conf['rate_control']
        max_retries = llm_agent_conf['max_retries'] if not rate_control else 0

        use_cache = llm_agent_conf['use_cache']
        cache_file_name = llm_agent_conf['cache_file_name']

        streaming = llm_agent_conf['streaming']

        real_agent = LLmAgent(base_url, model_name, api_key, timeout=timeout, max_retries=max_retries,
                              use_cache=use_cache, cache_file_name=cache_file_name, streaming=streaming)

        if rate_control:
            rate_control_conf = conf.get_conf()[rate_control]
            rpm = rate_control_conf['rpm']
            tpm = rate_control_conf['tpm']
            wait_seconds = rate_control_conf['wait_seconds']
            max_retry = rate_control_conf['max_retry']
            window_gap = rate_control_conf['window_gap']
            token_encoding = rate_control_conf['token_encoding']
            rate_control_agent = RateControl(real_agent, rpm=rpm, tpm=tpm, wait_seconds=wait_seconds,
                                             max_retry=max_retry, window_gap=window_gap, token_encoding=token_encoding)
            self.agent_dict[llm_agent_name] = rate_control_agent
            return rate_control_agent
        else:
            self.agent_dict[llm_agent_name] = real_agent
            return real_agent


llm_agent_factory = LLmAgentFactory()


if __name__ == '__main__':
    # prompt = ("以下是markdown格式的一些标题，它们是从一篇文章中抽出来的，但是丢失了原来的目录级别，全变成一级标题了，你能帮我从它们的序号、含义、前后关联，还原出原来的层级吗？"
    #           "还是按照markdown的格式返回，只修改标题级别（即修改#符的个数），markdown中的标题范围为1-6级。章(chapter)标题的级别一般为1级。"
    #           "不要修改任何其它内容，不要调整标题的前后顺序，不要使用code wrapper (```)，直接返回结果，不要给任何解释或描述：\n{headers}")
    #
    # with open(r'D:\workPython\llm\llm_translate\output\test\test_title4.txt', 'rt', encoding='utf-8') as f:
    #     headers = f.read()
    #
    # print(llm_agent_factory.generate('llm_agent').ask_llm(prompt, headers))

    # prompt = ("以下是markdown格式的一些标题，它们是从一篇文章中抽出来的，但是丢失了原来的目录级别，全变成一级标题了，你能帮我从它们的序号、含义、前后关联，还原出原来的1、2级目录吗？"
    #           "还是按照markdown的格式返回，章(chapter)标题的级别一般为1级，只保留1、2级目录，其它的丢弃。"
    #           "不要调整标题的前后顺序，不要使用code wrapper (```)，直接返回结果，不要给任何解释或描述：\n{headers}")
    #
    # with open(r'D:\workPython\llm\llm_translate\output\test\big_catalogs.txt', 'rt', encoding='utf-8') as f:
    #     headers = f.read()
    #
    # print(llm_agent_factory.generate('powerful_llm_agent').ask_llm(prompt, headers))


    prompt = ("这是一个markdown文件中的一段，把它翻译成中文，不要修改任何markdown标记，不要使用code wrapper (```)，"
              "这个片段中可能会插入图片链接及图片标题、html表格及表格标题，导致某段文字内容被分隔开，翻译的时候尽量不要丢失内容。"
              "这个片段中可能会插入代码，但是代码可能丢失了缩进空格，帮我补充一下缩进，并且在代码开头和结尾加上对应的code wrapper(比如开头加上```cpp，结尾加上```，注意只包代码片段，不要把多余的内容包进来)。"
              "除上述要求之外，翻译原文即可，不要在末尾接龙。"
              "直接返回翻译结果，不要给任何解释或描述：\n{content}")

    with open(r'D:\workPython\llm\llm_translate\output\test\trans6.txt', 'rt', encoding='utf-8') as f:
        headers = f.read()

    print(llm_agent_factory.generate('doubao_llm_agent').ask_llm(prompt, headers))

    # prompt = ("下面这段文本是在pdf中提取出来，转为markdown格式，请帮我分类一下，分类条目为：1.包含代码，2.不包含代码。注意：a.数学公式不算代码。"
    #           "b.文本格式可能有点乱，不需要是完全正确、可运行的代码，只要包含了比如python、c++之类的代码语句，就算是包含代码。"
    #           "直接给出分类条目的编号，不要给任何解释：\n{content}")
    #
    # # prompt = ("下面这段文本是从markdown中截取出来的，请帮我分类一下，分类条目为：1.主要包含代码，2.主要包含公式，3.既无代码也无公式，"
    # #           "直接给出分类条目的编号，不要给任何解释：\n{content}")
    #
    # with open(r'D:\workPython\llm\llm_translate\output\test_classify\classify1.txt', 'rt', encoding='utf-8') as f:
    #     headers = f.read()
    #
    # print(llm_agent_factory.generate('free_llm_agent').ask_llm(prompt, headers))
