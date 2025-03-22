import logging

from llm_translate.config import conf


LOGGER = logging.getLogger(__name__)

class CodeFomatter:
    def __init__(self):
        code_format_conf = conf.get_conf()['code_format']
        self.languages: list = code_format_conf['languages']
        self.line_length = code_format_conf['line_length']
        self.split_before_chars = set(code_format_conf['split_before_chars'])
        self.split_after_chars = set(code_format_conf['split_after_chars'])
        self.code_wraper = '```'
        self.coda_wraper_start = {self.code_wraper + language for language in self.languages}
        self.format_all_code = code_format_conf['format_all_code']
        self.remove_code_in_line = code_format_conf['remove_code_in_line']
        self.code_in_line_length = code_format_conf['code_in_line_length']
        self.force_split = code_format_conf['force_split']
        self.indentation = code_format_conf['indentation']

    def split_line(self, line):
        if len(line) < self.line_length:
            return [line]

        split_lines = []
        splited_num = 0

        line_length = self.line_length

        first_indentation = 0
        for c in line:
            if c == ' ':
                first_indentation += 1
            else:
                break

        indentation_start = ''

        while splited_num + line_length < len(line):
            cur_split_line = None
            for index in range(line_length - 1 + splited_num, splited_num - 1, -1):
                if line[index] in self.split_before_chars and index > splited_num:
                    cur_split_line = line[splited_num:index]
                    splited_num += index - splited_num
                    break
                elif line[index] in self.split_after_chars:
                    cur_split_line = line[splited_num:index + 1]
                    splited_num += index - splited_num + 1
                    break
            else:
                if self.force_split:
                    cur_split_line = line[splited_num:line_length + splited_num]
                    splited_num += line_length
                else:
                    LOGGER.error('cat not split: %s', line)
                    break

            if indentation_start:
                split_lines.append(indentation_start + cur_split_line + '  \n')
            else:
                split_lines.append(cur_split_line + '  \n')
                indentation_start = ' ' * (first_indentation + self.indentation)
                line_length -= first_indentation + self.indentation

        if splited_num < len(line):
            split_lines.append(indentation_start + line[splited_num:])
        return split_lines

    def correct_line(self, line):
        if not self.remove_code_in_line:
            return line

        in_line_code = False  # 一行文字内也可包含代码，比如`create_network()`
        code_wraper_char = '`'
        max_code_length = 0
        cur_code_length = 0
        for index in range(len(line)):
            c = line[index]
            if not in_line_code:
                if c == code_wraper_char:
                    in_line_code = True
            else:
                if c == code_wraper_char:
                    max_code_length = max(max_code_length, cur_code_length)
                    cur_code_length = 0
                else:
                    cur_code_length += 1

        # 这里单独用一个code_in_line_length来作长度上限，是因为这一行有可能处在多级列表之中，会有缩进，但缩进在pdf中导致占多长，不太好算
        # 所以先粗略额外定一个长度上限
        if max_code_length > self.code_in_line_length:
            line = line.replace(code_wraper_char, '')

        return line

    def is_enter_code(self, line):
        line = line.strip()
        if self.format_all_code:
            return line.startswith(self.code_wraper)

        return line in self.coda_wraper_start

    def do_correct(self, md_path, correct_md_path, encoding='utf-8'):
        result_lines = []

        with open(md_path, 'rt', encoding=encoding, newline='') as f:
            in_code = False
            for line in f:
                if in_code:
                    if line.strip() == self.code_wraper:
                        result_lines.append(line)
                        in_code = False
                    else:
                        if len(line) > self.line_length:
                            result_lines.extend(self.split_line(line))
                        else:
                            result_lines.append(line)
                else:
                    if self.is_enter_code(line):
                        result_lines.append(line)
                        in_code = True
                    else:
                        result_lines.append(self.correct_line(line))

        with open(correct_md_path, 'wt', encoding='utf-8', newline='') as f:
            f.write(''.join(result_lines))


if __name__ == '__main__':
    formatter = CodeFomatter()
    formatter.do_correct(r'D:\workPython\llm\llm_translate\output\Hands-On Large Language Models Language Understanding and Generation\Hands-On Large Language Models Language Understanding and Generation_correct_trans.md',
                         r'D:\workPython\llm\llm_translate\output\Hands-On Large Language Models Language Understanding and Generation\Hands-On Large Language Models Language Understanding and Generation_correct_trans_format.md')



