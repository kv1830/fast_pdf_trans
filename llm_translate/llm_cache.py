from threading import Lock
from pathlib import Path
import logging
from collections import defaultdict

LOGGER = logging.getLogger(__name__)


class LLmCache:
    lock_pool = defaultdict(Lock)

    def __init__(self, cache_path):
        self.cache_path = Path(cache_path).resolve()
        self.cache_dict = {}
        self.lock = LLmCache.lock_pool[str(self.cache_path)]
        self.lines = None
        self.index = 0
        self.read_state = 'k_start'
        self.current_key = None

        self.state_dict = {'k_start': self.__k_start__, 'k_value': self.__k_value__,
                           'v_start': self.__v_start__, 'v_value': self.__v_value__,
                           'total_end': None}

        if self.cache_path.exists():
            self.reload()
        else:
            self.cache_path.parent.mkdir(exist_ok=True, parents=True)

    def __k_start__(self):
        line = self.lines[self.index]

        if not line.strip():
            self.read_state = 'total_end'
            LOGGER.warning(f'get empty line when look for k_start, finish read, index: {self.index}')
            return

        if line.strip() == '|*||*||*|key_start|*||*||*|':
            self.read_state = 'k_value'
            self.index += 1
        else:
            raise ValueError(f'not found key_start, index: {self.index}, line: {line}')

    def __k_value__(self):
        key_value_list = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip() != '|*||*||*|key_end|*||*||*|':
                key_value_list.append(line)
            else:
                self.read_state = 'v_start'
                self.current_key = '\n'.join(key_value_list)
                break
        else:
            raise ValueError(f'not found k_end util index: {self.index}')

    def __v_start__(self):
        line = self.lines[self.index]
        if line.strip() == '|*||*||*|value_start|*||*||*|':
            self.read_state = 'v_value'
            self.index += 1
        else:
            raise ValueError(f'not found value_start, index: {self.index}, line: {line}')

    def __v_value__(self):
        value_value_list = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            if line.strip() != '|*||*||*|value_end|*||*||*|':
                value_value_list.append(line)
            else:
                self.read_state = 'k_start'
                self.cache_dict[self.current_key] = '\n'.join(value_value_list)
                break
        else:
            raise ValueError(f'not found v_end util index: {self.index}')

    def reload(self):
        self.lock.acquire_lock()
        self.cache_dict = {}
        with open(self.cache_path, 'rt', encoding='utf-8', newline='') as f:
            txt = f.read()
            self.lines = txt.split('\n')

        while self.index < len(self.lines):
            func = self.state_dict[self.read_state]
            if func:
                func()
            else:
                break

        self.lock.release_lock()

    def save_all(self):
        self.lock.acquire_lock()
        with open(self.cache_path, 'wt', encoding='utf-8', newline='') as f:
            for k, v in self.cache_dict.items():
                f.write('|*||*||*|key_start|*||*||*|\n')
                f.write(k + '\n')
                f.write('|*||*||*|key_end|*||*||*|\n')

                f.write('|*||*||*|value_start|*||*||*|\n')
                f.write(v + '\n')
                f.write('|*||*||*|value_end|*||*||*|\n')
        self.lock.release_lock()

    def save_one(self, k, v):
        self.lock.acquire_lock()
        self.cache_dict[k] = v
        with open(self.cache_path, 'at', encoding='utf-8', newline='') as f:
            f.write('|*||*||*|key_start|*||*||*|\n')
            f.write(k + '\n')
            f.write('|*||*||*|key_end|*||*||*|\n')

            f.write('|*||*||*|value_start|*||*||*|\n')
            f.write(v + '\n')
            f.write('|*||*||*|value_end|*||*||*|\n')
        self.lock.release_lock()

    def get(self, k):
        if k in self.cache_dict:
            return self.cache_dict[k]
        else:
            return None

    def __contains__(self, k):
        return k in self.cache_dict


if __name__ == '__main__':
    import pickle
    with open(r'D:\workPython\llm\llm_translate\cache\deepseek-chat.pkl', 'rb') as f:
        test_dict = pickle.load(f)

    llmcache = LLmCache(r'D:\workPython\llm\llm_translate\cache\deepseek-chat.txt')
    llmcache.cache_dict = test_dict
    llmcache.save_all()

    llmcache1 = LLmCache(r'D:\workPython\llm\llm_translate\cache\deepseek-chat.txt')
    test_dict1 = llmcache1.cache_dict
    print(test_dict == test_dict1)