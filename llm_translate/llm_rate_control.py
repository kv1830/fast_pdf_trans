from datetime import datetime, timedelta
from threading import Lock
import logging
import time
import collections
import sys

import tiktoken

from langchain_core.prompts import PromptTemplate

from openai import RateLimitError

# FORMAT = '%(asctime)-s %(name)s %(funcName)s %(lineno)d %(levelname)-8s %(message)s'
# logging.basicConfig(format=FORMAT, stream=sys.stderr, level=logging.INFO)

LOGGER = logging.getLogger(__name__)

RequestRecord = collections.namedtuple('RequestRecord', ['record_time', 'token_num'])


class RateControl:
    def __init__(self, real_agent, rpm=1000, tpm=10000, wait_seconds=30,
                 max_retry=5, window_gap=60, token_encoding='cl100k_base'):
        self.last_minute = None
        self.count = 0
        self.tokens = 0
        self.rpm = rpm
        self.tpm = tpm
        self.wait_seconds = wait_seconds
        self.max_retry = max_retry
        self.window_gap = window_gap
        self.lock = Lock()

        self.real_agent = real_agent

        self.records = []
        self.start_index = 0
        self.token_encoding = token_encoding

    def shrink_window(self, current_time: datetime):
        old_record = self.records[self.start_index]
        old_time = old_record.record_time
        time_delta = current_time - old_time

        while time_delta.days > 0 or time_delta.seconds > self.window_gap:
            self.start_index += 1
            self.count -= 1
            self.tokens -= old_record.token_num

            if self.start_index >= len(self.records):
                self.clear_record()
                return

            old_record = self.records[self.start_index]
            old_time = old_record.record_time
            time_delta = current_time - old_time

        if self.start_index > 10000:
            self.records = self.records[self.start_index:]
            self.start_index = 0

    def add_record(self, current_time, token_num):
        self.tokens += token_num
        self.count += 1
        self.records.append(RequestRecord(current_time, token_num))

    def clear_record(self):
        self.records = []
        self.count = 0
        self.tokens = 0
        self.start_index = 0

    def need_block_without_lock(self, token_num):
        if not self.records:
            self.add_record(datetime.now(), token_num)
            self.start_index = 0
            return False
        else:
            current_time = datetime.now()
            self.shrink_window(current_time)

            if self.tokens + token_num > self.tpm:
                LOGGER.warning('reach max tpm, current tokens: %d, current token_num: %d', self.tokens, token_num)
                return True
            elif self.count + 1 > self.rpm:
                LOGGER.warning('reach max rpm, current count: %d', self.count)
                return True
            else:
                self.add_record(datetime.now(), token_num)
                return False

    def calc_token_num(self, prompt, prompt_var):
        prompt_str = self.real_agent.format_prompt(prompt, prompt_var)

        enc = tiktoken.get_encoding(self.token_encoding)

        return len(
            enc.encode(
                prompt_str,
                allowed_special='all',
                disallowed_special=(),
            )
        )

    def need_block(self, token_num):
        if self.rpm == 0 and self.tpm == 0:
            return False

        self.lock.acquire_lock()
        try:
            result = self.need_block_without_lock(token_num)
        finally:
            self.lock.release_lock()

        return result

    def do_block_with_retry(self, prompt, prompt_var, token_num):
        try:
            max_retry = self.max_retry
            while self.need_block(token_num):
                if max_retry <= 0:
                    LOGGER.error('reach max_retry, failed, prompt: %s, prompt_var: %s', prompt, prompt_var[:100])
                    return False

                time.sleep(self.wait_seconds)
                max_retry -= 1
        except:
            LOGGER.exception("ask_llm occur exeption, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            return False
        return True

    def ask_llm_no_block(self, prompt, prompt_var):
        try:
            result = self.real_agent.ask_llm(prompt, prompt_var)
        except:
            LOGGER.exception("ask_llm occur exeption, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            result = None
        return result

    def ask_llm_after_RateLimitError(self, prompt, prompt_var, token_num):
        self.lock.acquire_lock()
        try:
            self.records = [RequestRecord(datetime.now(), self.tpm)]
            self.count = 1
            self.tokens = self.tpm
        finally:
            self.lock.release_lock()

        block_result = self.do_block_with_retry(prompt, prompt_var, token_num)
        if not block_result:
            return 'ask_llm reach max_retry, failed'

        try:
            result = self.ask_llm_no_block(prompt, prompt_var)
        except RateLimitError:
            LOGGER.exception("ask_llm occur RateLimitError, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            result = None
        except:
            LOGGER.exception("ask_llm occur exception, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            result = None

        return result

    def ask_llm(self, prompt, prompt_var):
        if self.rpm == 0 and self.tpm == 0:
            return self.ask_llm_no_block(prompt, prompt_var)

        # 缓存命中则不计流控
        if self.real_agent.in_cache(prompt, prompt_var):
            return self.ask_llm_no_block(prompt, prompt_var)

        token_num = self.calc_token_num(prompt, prompt_var)
        block_result = self.do_block_with_retry(prompt, prompt_var, token_num)
        if not block_result:
            LOGGER.error('ask_llm reach max_retry, failed')
            return None

        LOGGER.info('block finish, prompt: %s, prompt_var: %s', prompt, prompt_var[:100])

        try:
            result = self.ask_llm_no_block(prompt, prompt_var)
        except RateLimitError:
            LOGGER.exception("ask_llm occur RateLimitError, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            return self.ask_llm_after_RateLimitError(prompt, prompt_var, token_num)
        except:
            LOGGER.exception("ask_llm occur exception, prompt: %s, prompt_var: %s", prompt, prompt_var[:100])
            return None

        LOGGER.info('ask_llm finish, prompt: %s, prompt_var: %s', prompt, prompt_var[:100])
        return result

