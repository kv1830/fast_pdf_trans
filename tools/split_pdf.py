import fitz
import math

from collections import OrderedDict


def get_bookmarks_with_coordinates(doc):
    """提取书签及其坐标信息"""
    bookmarks = OrderedDict()
    outline = doc.outline  # 获取大纲（书签）的根节点

    parent_bks = []
    parent_bk_indexs = []
    index = -1

    while outline:
        if index > 10000:
            break

        index += 1
        page_num = outline.page  # 目标页码（0-based）
        # 获取坐标（处理不同目标类型）
        x, y = outline.x, outline.y

        grade_num = sum([1 if (0 <= bk.page < doc.page_count) else 0 for bk in parent_bks]) + 1
        cur_bk = {
            'title': outline.title,
            'page': page_num,
            'x': x,
            'y': y,
            'grade': grade_num,
            'parent_index': parent_bk_indexs[-1] if parent_bk_indexs else -1
        }

        if page_num in bookmarks:
            bookmarks[page_num].append(cur_bk)
        else:
            bookmarks[page_num] = [cur_bk]
        # print(f'index: {index}, cur_bk: {cur_bk}')

        if outline.down:
            parent_bks.append(outline)
            parent_bk_indexs.append(index)
            outline = outline.down
            continue
        elif outline.next:
            outline = outline.next
            continue

        while parent_bks:
            outline = parent_bks.pop()
            parent_bk_indexs.pop()
            if outline.next:
                outline = outline.next
                break
        else:
            outline = None

    return bookmarks


def get_parents_bookmarks(pageid_2_bookmarks, parent_index, start_page):
    merged_bookmarks = []
    for bookmarks in pageid_2_bookmarks.values():
        merged_bookmarks.extend(bookmarks)

    parents_bks = []
    while parent_index != -1:
        bk = merged_bookmarks[parent_index]
        grade, title, page, x, y, parent_index = bk['grade'], bk['title'], bk['page'], bk['x'], bk['y'], bk['parent_index']
        page_num = page + 2 - start_page # 因为page本身是从0开始计数的，而page_num需要从1开始计数，start_page也是从1开始计数

        dest_dict = {
            "kind": fitz.LINK_GOTO,
            "page": page_num,
            "to": fitz.Point(0, y),
        }
        parents_bks.append([grade, title, 0, None])
    return list(reversed(parents_bks))


def split_with_bookmarks(pdf_path, dest_pdf_path, start_page, end_page):
    doc = fitz.open(pdf_path)

    pageid_2_bookmarks = get_bookmarks_with_coordinates(doc)

    total_bookmarks = []

    min_grade = 9999
    for page_id, bookmarks in pageid_2_bookmarks.items():
        page_num = page_id + 1

        if page_num < start_page:
            continue

        if page_num > end_page:
            break

        page_num = page_num - start_page + 1

        for bk in bookmarks:
            grade, title, x, y, parent_index = bk['grade'],  bk['title'],  bk['x'],  bk['y'],  bk['parent_index']
            dest_dict = {
                "kind": fitz.LINK_GOTO,
                "page": page_num,
                "to": fitz.Point(0 if math.isnan(x) else x, 0 if math.isnan(y) else y),
            }

            # if not total_bookmarks and parent_index != -1:
            #     total_bookmarks.extend(get_parents_bookmarks(pageid_2_bookmarks, parent_index, start_page))
            min_grade = min(min_grade, grade)
            total_bookmarks.append([grade - min_grade + 1, title, page_num, dest_dict])

    for bk in total_bookmarks:
        print(bk)

    split_doc = fitz.open()
    split_doc.insert_pdf(doc, from_page=start_page - 1, to_page=end_page - 1)
    split_doc.set_toc(total_bookmarks)
    split_doc.save(dest_pdf_path)

    doc.close()


if __name__ == '__main__':
    # split_bookmarks(r"D:\学习\机器学习\GAN、扩散模型\Diffusion Models-283-285.pdf",
    #                dest_pdf_path=r"D:\学习\机器学习\GAN、扩散模型\Diffusion Models-283-285-test.pdf")

    split_with_bookmarks(r"D:\学习\机器学习\自然语言处理\Building LLM Apps Create Intelligent Apps and Agents with Large Language Models.pdf",
                   r"D:\学习\机器学习\自然语言处理\Building LLM Apps Create Intelligent Apps and Agents with Large Language Models-22-23.pdf", 22, 23)

