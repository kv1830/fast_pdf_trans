import cv2
from paddleocr import PaddleOCR

ocr_model = None

def ocr_to_txts(img):
    global ocr_model

    if ocr_model is None:
        # need to run only once to download and load model into memory
        ocr_model = PaddleOCR(use_angle_cls=True, lang="en", precision="fp16",
                        det_limit_side_len=960,
                        # det_model_dir=r"D:\ai\ocr\ch_PP-OCRv4_det_server_infer",
                        # rec_model_dir=r"D:\ai\ocr\ch_PP-OCRv4_rec_server_infer",
                        )

    result = ocr_model.ocr(img, cls=True)
    # 显示结果
    result = result[0]

    if not result:
        return ''

    for line in result:
        print(line)

    txts = ' '.join([line[1][0] for line in result])
    return txts


if __name__ == '__main__':
    print(ocr_to_txts(cv2.imread(r'D:\tmp\screen\test\df3.png')))


