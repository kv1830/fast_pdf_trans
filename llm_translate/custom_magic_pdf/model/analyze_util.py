import threading

analyse_local_data = threading.local()


def is_contains_code(analyze_agent, res_txts):
    prompt = ("下面这段文本是在pdf中提取出来，转为markdown格式，请帮我分类一下，分类条目为：1.包含代码，2.不包含代码。注意：a.数学公式不算代码。"
              "b.文本格式可能有点乱，不需要是完全正确、可运行的代码，只要包含了比如python、c++之类的代码语句，就算是包含代码。"
              "直接给出分类条目的编号，不要给任何解释：\n{content}")

    llm_result: str = analyze_agent.ask_llm(prompt, res_txts)
    llm_result = llm_result.split('</think>')[-1].replace(' ', '')
    contains_code = llm_result in ['1', '1.包含代码', '1包含代码']
    return contains_code


def remove_fomular_from_code(useful_list, adjusted_mfdetrec_res, layout_res):
    paste_x, paste_y, xmin, ymin, xmax, ymax, new_width, new_height = useful_list

    for mfd_res in adjusted_mfdetrec_res:
        x0, y0, x1, y1 = mfd_res['bbox']
        mf_xmin = x0 + xmin - paste_x
        mf_ymin = y0 + ymin - paste_y
        mf_xmax = x1 + xmin - paste_x
        mf_ymax = y1 + ymin - paste_y

        layout_res_index = 0
        while layout_res_index < len(layout_res):
            per_layout_res = layout_res[layout_res_index]
            res_poly = per_layout_res['poly']
            res_type = per_layout_res['category_id']
            if res_type not in [13, 14]:
                layout_res_index += 1
                continue

            if mf_xmin == res_poly[0] and mf_ymin == res_poly[1] and mf_xmax == res_poly[4] and mf_ymax == res_poly[5]:
                layout_res.pop(layout_res_index)
            else:
                layout_res_index += 1
    adjusted_mfdetrec_res.clear()