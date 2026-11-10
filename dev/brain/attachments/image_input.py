# -*- coding: utf-8 -*-
"""为模型输入生成缩放后的图片 data URL。"""

import base64
import io


def image_to_data_url(path: str, *, max_side: int, quality: int) -> str:
    from PIL import Image

    with Image.open(path) as image, io.BytesIO() as buffer:
        image.thumbnail((max_side, max_side))
        if image.mode in ("RGB", "L", "CMYK"):
            image.save(buffer, format="JPEG", quality=quality)
        else:
            with image.convert("RGB") as converted:
                converted.save(buffer, format="JPEG", quality=quality)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    return f"data:image/jpeg;base64,{encoded}"
