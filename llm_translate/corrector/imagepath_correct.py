import logging
import re

LOGGER = logging.getLogger(__name__)

class ImagePathCorrector:
    def __init__(self):
        pass

    def get_all_spans(self, cotent, pattern: re.Pattern):
        spans = []
        m = pattern.search(cotent)
        while m:
            cur_span = m.span()
            spans.append(cur_span)
            m = pattern.search(cotent, cur_span[1])
        return spans

    def correct_imagepath(self, src_str, dest_str):
        p = re.compile(r'\!\[\]\(images/\w+.jpg\)')

        src_spans = self.get_all_spans(src_str, p)
        dest_spans = self.get_all_spans(dest_str, p)

        src_count = len(src_spans)
        dest_count = len(dest_spans)
        if src_count != dest_count:
            LOGGER.error(f'image count not equal, {src_count} != {dest_count} src_str: {src_str}, dest_str: {dest_str}')
            raise ValueError(f'image count not equal, {src_count} != {dest_count}')

        for src_span, dest_span in zip(reversed(src_spans), reversed(dest_spans)):
            src_span_content = src_str[src_span[0]: src_span[1]]
            dest_span_content = dest_str[dest_span[0]: dest_span[1]]

            if src_span_content != dest_span_content:
                LOGGER.warning(f'correct image path from {src_span_content} -> {dest_span_content}')
            dest_str = dest_str[:dest_span[0]] + src_span_content + dest_str[dest_span[1]:]
        return dest_str