import magic_pdf.pre_proc.ocr_dict_merge as origin_ocr_dict_merge

from magic_pdf.config.ocr_content_type import BlockType, ContentType


def span_block_type_compatible(span_type, block_type):
    if span_type in [ContentType.Text, ContentType.InlineEquation]:
        return block_type in [BlockType.Text, BlockType.Title, BlockType.ImageCaption, BlockType.ImageFootnote,
                              BlockType.TableCaption, BlockType.TableFootnote,
                              BlockType.Discarded,  # 匹配书签要用
                              ]
    elif span_type == ContentType.InterlineEquation:
        return block_type in [BlockType.InterlineEquation]
    elif span_type == ContentType.Image:
        return block_type in [BlockType.ImageBody]
    elif span_type == ContentType.Table:
        return block_type in [BlockType.TableBody]
    else:
        return False


origin_ocr_dict_merge.span_block_type_compatible = span_block_type_compatible