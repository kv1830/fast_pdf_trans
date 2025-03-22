import re
import logging

from llm_translate.llm_agent import llm_agent_factory
from llm_translate.edit_distance import MinDistance
from llm_translate.config import conf


LOGGER = logging.getLogger(__name__)


class EasyHeaderCorrector:
    def __init__(self):
        corrector_conf = conf.get_conf()['header_corrector']
        self.allow_diff_chars = corrector_conf['allow_diff_chars']

        if self.allow_diff_chars is None:
            self.allow_diff_chars = []

        self.allow_distance = corrector_conf['allow_distance']
        self.header_p = re.compile("^#{1,6} (.+)$")
        self.llm_agent = llm_agent_factory.generate(corrector_conf['llm_agent_name'])

    def is_same(self, src_header, dest_header):
        if dest_header.strip() == src_header.strip():
            return True

        minD = MinDistance()
        distance = minD.min_distance(src_header.strip(), dest_header.strip())
        edit_trace = minD.get_edit_trace(need_skip=False)
        LOGGER.info('edit_trace: %s, src_header: %s, dest_header: %s', edit_trace, src_header, dest_header)
        for diff_char, edit_type in minD.get_edit_trace(need_skip=False):
            # 白名单内的字符不算距离
            if diff_char in self.allow_diff_chars:
                distance -= 1

        if distance > self.allow_distance:
            LOGGER.warning("distance > allow_distance break, distance: %d, allow_distance: %d, src_header: %s, dest_header: %s",
                         distance, self.allow_distance, src_header, dest_header)
            return False
        return True

    def is_same_without_grade(self, src_header, dest_header):
        src_header = self.remove_header_format(src_header)
        dest_header = self.remove_header_format(dest_header)
        return self.is_same(src_header, dest_header)

    def correct_header_section(self, correct_headers, header_lines, lines):
        correct_index = 0
        total_index = 0

        while correct_index < len(correct_headers):
            correct_line = correct_headers[correct_index]
            m = re.match(self.header_p, correct_line)
            if not m:
                correct_index += 1
                continue

            correct_header_content = m.group(1)
            total_index_backup = total_index

            while total_index < len(header_lines):
                header_index, header_line = header_lines[total_index]
                header_content = header_line[2:]

                if correct_header_content.strip() == header_content.strip() or self.is_same(header_content, correct_header_content):
                    lines[header_index] = correct_line.rstrip() + '  \n'
                    total_index += 1
                    break

                # 通过前一个匹配上的correct_header的级别来判断当前丢失的标题级别下限，不一定准确。但是用deepseek的时候发现，它倾向于丢失5级标题。
                LOGGER.warning("correct_line and origin not equal! correct_line: %s, origin: %s, drop origin",
                               correct_line, header_line)

                lines[header_index] = self.remove_header_format(header_line)
                LOGGER.warning('remove_header_format, header_index: %d, header_line: %s, remove_result: %s',
                               header_index, header_line, lines[header_index])

                total_index += 1
            else:
                LOGGER.warning("correct_line doesn't match any line, drop it: %s", correct_line)
                total_index = total_index_backup

            correct_index += 1

        while total_index < len(header_lines):
            header_index, header_line = header_lines[total_index]
            lines[header_index] = self.remove_header_format(header_line)
            LOGGER.warning('drop remaining header_line: %s, remove_result: %s', header_line, lines[header_index])

            total_index += 1

    def get_header_grade(self, header_line):
        m = re.match("^(#{1,6}) .+$", header_line)
        if not m:
            return -1

        return len(m.group(1))

    def remove_header_format(self, header_line):
        grade = self.get_header_grade(header_line)
        if grade > 0:
            return header_line[grade + 1:]

        return header_line

    def correct_correct(self, correct_headers):
        correct_correct_headers = []
        for index, header in enumerate(correct_headers):
            m = re.match("^(#{1,6}) .+$", header)
            if not m:
                LOGGER.warning('format error, drop correct_header, index: %d, header: %s', index, header)
                continue

            if not header.endswith('  \n'):
                header = header.strip() + '  \n'

            correct_correct_headers.append(header)

        return correct_correct_headers

    def do_correct(self, md_path, correct_md_path, encoding='utf-8'):
        header_lines = []
        with open(md_path, 'rt', encoding=encoding, newline='') as f:
            lines = f.readlines()

        for index, line in enumerate(lines):
            m = re.match(self.header_p, line)
            if m:
                if not m.group(1).strip():
                    LOGGER.info('drop line: %s, index: %d', line, index)
                    lines[index] = ''
                    continue

                header_lines.append((index, line))

        header_txts = "".join([header[1] for header in header_lines])
        prompt = ("以下是markdown格式的一些标题，它们是从一篇文章中抽出来的，但是丢失了原来的目录级别，全变成一级标题了，你能帮我从它们的序号、含义、前后关联，还原出原来的1、2级目录吗？"
                  "还是按照markdown的格式返回，章(chapter)标题的级别一般为1级，只保留1、2级目录，其它的丢弃。"
                  "不要调整标题的前后顺序，不要使用code wrapper (```)，直接返回结果，不要给任何解释或描述：\n{headers}")

        LOGGER.info('header_txts: %s', header_txts)

        correct_header_txts = self.llm_agent.ask_llm(prompt, header_txts)
        LOGGER.info('correct_header_txts: %s', correct_header_txts)
        if not correct_header_txts:
            LOGGER.error('llm response is empty, correct header failed')
            return False

        correct_headers = correct_header_txts.split('\n')
        if not correct_headers:
            LOGGER.error('correct_headers is empty, correct header failed')
            return False

        correct_headers = self.correct_correct(correct_headers)

        self.correct_header_section(correct_headers, header_lines, lines)

        with open(correct_md_path, 'wt', encoding=encoding, newline='') as f:
            f.write(''.join(lines))

        LOGGER.info('correct header finish: %s', correct_md_path)
        return True