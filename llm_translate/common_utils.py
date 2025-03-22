import re


def extract_md_headers(md_path, encoding='utf-8'):
    header_lines = []
    with open(md_path, 'rt', encoding=encoding) as f:
        for index, line in enumerate(f):
            if re.match("^#{1,6} \\w+\\s*", line):
                header_lines.append(line)

    return ''.join(header_lines)


if __name__ == '__main__':
    line = '# aaaa \n'
    print(re.match("^#{1,6} \\w+\\s*", line))