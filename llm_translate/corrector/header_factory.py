from llm_translate.corrector.header import HeaderCorrector
from llm_translate.corrector.easy_header import EasyHeaderCorrector

class HeaderFactory:
    def __init__(self, easy_header):
        self.easy_header = easy_header

    def generate(self):
        if self.easy_header:
            return EasyHeaderCorrector()
        else:
            return HeaderCorrector()