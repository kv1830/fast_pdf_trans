import os
from pathlib import Path
import re
import logging

from llm_translate.llm_agent import llm_agent_factory
from llm_translate.edit_distance import MinDistance, EditType
from llm_translate.split_utils import split_md
from llm_translate.config import conf


LOGGER = logging.getLogger(__name__)


class HeaderCorrector:
    def __init__(self):
        corrector_conf = conf.get_conf()['header_corrector']
        self.allow_diff_chars = corrector_conf['allow_diff_chars']
        self.allow_distance = corrector_conf['allow_distance']
        self.title_chunk_size = corrector_conf['title_chunk_size']
        self.header_p = re.compile("^#{1,6} (.+)$")
        self.allow_miss_num = corrector_conf['allow_miss_num']  # 允许丢失的标题数
        self.allow_miss_num_add = 0
        self.allow_miss_depth = corrector_conf['allow_miss_depth']  # 允许丢失4级以上标题，不限数量。如果不允许此功能，则将allow_miss_depth设为7
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

    def correct_header_section(self, correct_headers, header_lines, lines, total_index):
        miss_num = 0
        correct_index = 0
        last_depth = -1

        while correct_index < len(correct_headers) and total_index < len(header_lines):
            correct_line = correct_headers[correct_index]
            m = re.match(self.header_p, correct_line)
            if not m:
                correct_index += 1
                continue

            correct_header_content = m.group(1)

            header_index, header_line = header_lines[total_index]
            header_content = header_line[2:]

            if correct_header_content.strip() != header_content.strip() and not self.is_same(header_content, correct_header_content):
                if last_depth >= self.allow_miss_depth:
                    # 通过前一个匹配上的correct_header的级别来判断当前丢失的标题级别下限，不一定准确。但是用deepseek的时候发现，它倾向于丢失5级标题。
                    LOGGER.warning("correct_line and origin not equal! correct_line: %s, origin: %s, miss_num: %d. "
                                   "but origin grade may >= %d, so is not include of the miss num",
                                   correct_line, header_line, miss_num, self.allow_miss_depth)

                    lines[header_index] = self.remove_header_format(header_line)
                    LOGGER.warning('remove_header_format, header_index: %d, header_line: %s, remove_result: %s',
                                   header_index, header_line, lines[header_index])

                    total_index += 1
                    continue

                if miss_num < self.allow_miss_num + self.allow_miss_num_add:
                    LOGGER.warning("correct_line and origin not equal! correct_line: %s, origin: %s, miss_num: %d, allow_miss_num: %d, allow_miss_num_add: %d",
                                   correct_line, header_line, miss_num, self.allow_miss_num, self.allow_miss_num_add)

                    lines[header_index] = self.remove_header_format(header_line)
                    LOGGER.warning('remove_header_format, header_index: %d, header_line: %s, remove_result: %s',
                                   header_index, header_line, lines[header_index])

                    miss_num += 1
                    total_index += 1
                    continue
                else:
                    LOGGER.error("correct_line and origin not equal, and miss_num bigger than allow! correct_line: %s, origin: %s, miss_num: %d",
                                   correct_line, header_line, miss_num)
                    return False, total_index

            lines[header_index] = correct_line.rstrip() + '  \n'
            total_index += 1
            correct_index += 1
            miss_num = 0
            last_depth = self.get_header_grade(correct_line)

        if correct_index < len(correct_headers) and total_index == len(header_lines):
            LOGGER.error("not found correct_line in origin: %s", correct_headers[correct_index])
            # 说明模型返回的标题在原标题中找不到
            return False, total_index

        return True, total_index

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

    def remove_error_grades(self, correct_headers):
        self.allow_miss_num_add = 0
        if len(correct_headers) <= 1:
            return correct_headers

        correct_correct_headers = [correct_headers[-1]]
        index_after = len(correct_headers) - 1
        index_before = len(correct_headers) - 2
        while index_before >= 0:
            header_after = correct_headers[index_after]
            header_before = correct_headers[index_before]
            grade_after = self.get_header_grade(header_after)
            grade_before = self.get_header_grade(header_before)
            if grade_after - grade_before > 1:
                LOGGER.warning('error grade, drop correct_header, index_before: %d, header_before: %s, index_after: %d, header_after: %s',
                               index_before, header_before, index_after, header_after)
                self.allow_miss_num_add += 1
                index_before -= 1
                continue

            correct_correct_headers.append(correct_headers[index_before])
            index_after = index_before
            index_before -= 1
        return list(reversed(correct_correct_headers))

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

        return self.remove_error_grades(correct_correct_headers)

    def merge_history_headers(self, correct_headers, history_headers, brother_num=4):
        LOGGER.debug('merge_history_headers start, correct_headers: %s, history_headers: %s',
                     ''.join(correct_headers), ''.join(history_headers))
        if history_headers:
            merged_headers = history_headers + correct_headers
        else:
            merged_headers = correct_headers

        last_grade = None
        reversed_history_headers = []
        saved_brother_num = brother_num
        for index, header in enumerate(reversed(merged_headers)):
            current_grade = self.get_header_grade(header)
            if current_grade == -1:
                LOGGER.error('error correct_header: %s, ignore it', header)
                continue

            if last_grade is None:
                last_grade = current_grade
                reversed_history_headers.append(header)
                continue

            if current_grade > last_grade:
                continue
            elif current_grade == last_grade:  # 保留brother_num个同级标题
                if saved_brother_num > 0:
                    reversed_history_headers.append(header)
                    saved_brother_num -= 1
                    continue
                else:
                    if last_grade == 1:  # 如果已经是1级了，并且已经保留了brother_num个同级标题，就可以结束了
                        break
            else:
                last_grade = current_grade
                saved_brother_num = brother_num
                reversed_history_headers.append(header)

        result_history_headers = list(reversed(reversed_history_headers))
        LOGGER.debug('merge_history_headers end, result_history_headers: %s', ''.join(result_history_headers))
        return result_history_headers

    def remove_overlap_his(self, history_headers, correct_headers, header_section):
        # 不管提示词是否要求模型不要把历史标题放在答复中，不同模型的表现并不一样，故先判断一下模型的答复中是否包含历史标题
        old_first_title = header_section.strip().split('\n')[0]
        if (not self.is_same_without_grade(history_headers[0], correct_headers[0])
                and self.is_same_without_grade(correct_headers[0], old_first_title)):
            return correct_headers

        his_index = 0
        for index, header in enumerate(correct_headers):
            m = re.match(self.header_p, header)
            if not m:
                LOGGER.info('drop error overlap header: %d', header)
                continue

            his_header = history_headers[index]
            his_header_grade = self.get_header_grade(his_header)
            header_grade = self.get_header_grade(header)
            if his_header_grade != header_grade:
                LOGGER.error('overlap his_header grade not equal, his_header_grade: %d, header_grade: %d, his_header: %s, header: %s',
                             his_header_grade, header_grade, his_header, header)
                return None

            if not self.is_same(his_header, header):
                LOGGER.error(
                    'overlap his_header not equal, his_header: s, header: %s', his_header, header)
                return None

            his_index += 1
            if his_index >= len(history_headers):
                return correct_headers[index + 1:]

        LOGGER.error("less correct_headers, history_headers: %s, correct_headers: %s",
                     ''.join(history_headers), ''.join(correct_headers))
        return None


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
        prompt = ("以下是markdown格式的一些标题，它们是从一篇文章中抽出来的，但是丢失了原来的目录级别，全变成一级标题了，你能帮我从它们的序号以及字面意思，"
                  "还原出原来的层级吗？还是按照markdown的格式返回，只修改标题级别（即修改#符的个数），不要修改任何其它内容，不要调整标题的前后顺序，"
                  "markdown中的标题范围为1-6级。章(chapter)标题的级别一般为1级。"
                  "不要使用code wrapper (```)，直接返回结果，不要给任何解释或描述：\n"
                  "//下面这些是已经修正过级别的历史标题，不要再修改：\n{history_headers}"
                  "//下面这些是需要修正级别的标题，第一条标题的级别不一定是1，你需要参照上面的历史标题：\n{headers}")

        prompt_without_history = ("以下是markdown格式的一些标题，它们是从一篇文章中抽出来的，但是丢失了原来的目录级别，全变成一级标题了，你能帮我从它们的序号以及字面意思，"
                                  "还原出原来的层级吗？还是按照markdown的格式返回，只修改标题级别（即修改#符的个数），不要修改任何其它内容，不要调整标题的前后顺序，"
                                  "markdown中的标题范围为1-6级。章(chapter)标题的级别一般为1级。"
                                  "不要使用code wrapper (```)，直接返回结果，不要给任何解释或描述：\n{headers}")

        LOGGER.info('header_txts: %s', header_txts)

        header_txts_list = split_md(header_txts, chunk_size=self.title_chunk_size)

        total_index = 0
        history_headers = []
        for section_index, header_section in enumerate(header_txts_list):
            header_section = header_section.lstrip()
            LOGGER.info('header_section: %s', header_section)

            if section_index == 0:
                correct_header_txts = self.llm_agent.ask_llm(prompt_without_history, header_section)
            else:
                correct_header_txts = self.llm_agent.ask_llm(prompt,
                                                             {'history_headers': ''.join(history_headers), 'headers': header_section})

            LOGGER.info('correct_header_txts: %s', correct_header_txts)

            if not correct_header_txts:
                LOGGER.error('llm response is empty, correct header failed')
                return False

            correct_headers = correct_header_txts.split('\n')
            correct_headers = self.correct_correct(correct_headers)

            if section_index > 0:
                correct_headers = self.remove_overlap_his(history_headers, correct_headers, header_section)
                LOGGER.debug('after remove_overlap_his: %s', ''.join(correct_headers) if correct_headers else '')
                if not correct_headers:
                    LOGGER.error('correct_headers is empty, correct header failed')
                    return False

            history_headers = self.merge_history_headers(correct_headers, history_headers)

            flag, total_index = self.correct_header_section(correct_headers, header_lines, lines, total_index)
            if not flag:
                LOGGER.error('correct_headers is empty, correct header failed')
                return False

        with open(correct_md_path, 'wt', encoding=encoding, newline='') as f:
            f.write(''.join(lines))

        LOGGER.info('correct header finish: %s', correct_md_path)
        return True