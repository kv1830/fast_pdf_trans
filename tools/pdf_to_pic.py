import fitz
import numpy as np
from PIL import Image
from pathlib import Path
import cv2


def fitz_doc_to_image(doc, dpi=200) -> dict:
    """Convert fitz.Document to image, Then convert the image to numpy array.

    Args:
        doc (_type_): pymudoc page
        dpi (int, optional): reset the dpi of dpi. Defaults to 200.

    Returns:
        dict:  {'img': numpy array, 'width': width, 'height': height }
    """

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pm = doc.get_pixmap(matrix=mat, alpha=False)

    # If the width or height exceeds 4500 after scaling, do not scale further.
    if pm.width > 4500 or pm.height > 4500:
        pm = doc.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)

    img = Image.frombytes('RGB', (pm.width, pm.height), pm.samples)
    img = np.array(img)

    img_dict = {'img': img, 'width': pm.width, 'height': pm.height}

    return img_dict


def pdf_2_pics(pdf_path,
               start_page=None, # 从1开始
               end_page=None, # 含end，即前闭后闭
               dpi=200
               ):
    docs = fitz.open(pdf_path)

    pdf_path = Path(pdf_path)
    pics_dir = pdf_path.parent / pdf_path.stem
    pics_dir.mkdir()

    for index, doc in enumerate(docs):
        page_index = index + 1

        if start_page and page_index < start_page:
            continue

        if end_page and page_index > end_page:
            break

        img = fitz_doc_to_image(doc, dpi=dpi)['img']
        cv2.imwrite(str(pics_dir / f'{page_index:04d}.png'), img)


if __name__ == '__main__':
    pdf_2_pics(r'C:\Users\kv183_pro\Documents\WeChat Files\wxid_ktouqnw0pwxh22\FileStorage\File\2025-03\On Food and Cooking The Science and Lore of the Kitchen-21.pdf')