from langchain_text_splitters.markdown import MarkdownHeaderTextSplitter
from langchain_text_splitters.character import RecursiveCharacterTextSplitter


def split_by_md_header(md_txt):
    headers_to_split_on = [('#', 'header1'), ('##', 'header2')]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on, return_each_line=False, strip_headers=False)
    return splitter.split_text(md_txt)


def split_by_tiktoken(md_txt, chunk_size, chunk_overlap=0):
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name='cl100k_base', chunk_size=chunk_size,
                                                                    chunk_overlap=chunk_overlap)
    return splitter.split_text(md_txt)


def split_documents_by_tiktoken(documents, chunk_size, chunk_overlap=0):
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name='cl100k_base',
                                                                    chunk_size=chunk_size,
                                                                    chunk_overlap=chunk_overlap,
                                                                    keep_separator='end',
                                                                    strip_whitespace=False)
    return splitter.split_documents(documents)


def split_md(md_txt, chunk_size, chunk_overlap=0, title_add_size=0):
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name='cl100k_base',
                                                                    separators=["\n#{1,6} "],
                                                                    keep_separator='start',
                                                                    is_separator_regex=True,
                                                                    chunk_size=chunk_size + title_add_size,
                                                                    chunk_overlap=chunk_overlap,
                                                                    strip_whitespace=False,
                                                                    allowed_special='all',
                                                                    disallowed_special=())

    sub_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name='cl100k_base',
                                                                    separators=["\n\n", "\n", " ", ""],
                                                                    keep_separator='end',
                                                                    is_separator_regex=False,
                                                                    chunk_size=chunk_size,
                                                                    chunk_overlap=chunk_overlap,
                                                                    strip_whitespace=False,
                                                                    allowed_special='all',
                                                                    disallowed_special=())
    documents = splitter.create_documents([md_txt])
    sub_documents = sub_splitter.split_documents(documents)
    splitted_texts = [document.page_content for document in sub_documents]
    return splitted_texts


if __name__ == '__main__':
    md_path = r'D:\workPython\llm\llm_translate\output\TensorRT-Developer-Guide\TensorRT-Developer-Guide_correct.md'
    split_path = r'D:\workPython\llm\llm_translate\output\TensorRT-Developer-Guide\TensorRT-Developer-Guide_correct_split.md'
    merged_path = r'D:\workPython\llm\llm_translate\output\TensorRT-Developer-Guide\TensorRT-Developer-Guide_correct_merge.md'

    with open(md_path, 'rt', encoding='utf-8', newline='') as f:
        md_text = f.read()
        texts = split_md(md_text, 2048)

    with open(split_path, 'wt', encoding='utf-8', newline='') as f:
        for index, text in enumerate(texts):
            f.write(f'**\n**\n{index}: --------------------------------------------------\n{text}')

    with open(merged_path, 'wt', encoding='utf-8', newline='') as f:
        f.write(''.join(texts))