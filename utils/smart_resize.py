import math

def smart_resize(height, width, factor=32, min_pixels=32*32*4, max_pixels=32*32*1280, max_long_side=8192):
    """
    计算模型内部缩放后的图像尺寸

    参数:
        height: 原始图像高度
        width: 原始图像宽度
        factor: 分辨率因子（固定为 16）
        min_pixels: 最小像素值
        max_pixels: 最大像素值
        max_long_side: 最长边限制

    返回:
        (h_bar, w_bar): 缩放后的高度和宽度
    """
    def round_by_factor(number, factor):
        return round(number / factor) * factor

    def ceil_by_factor(number, factor):
        return math.ceil(number / factor) * factor

    def floor_by_factor(number, factor):
        return math.floor(number / factor) * factor

    if height < 2 or width < 2:
        raise ValueError(f"height:{height} or width:{width} must be larger than factor:{factor}")
    elif max(height, width) / min(height, width) > 200:
        raise ValueError(f"absolute aspect ratio must be smaller than 200, got {height} / {width}")

    # 限制最长边
    if max(height, width) > max_long_side:
        beta = max(height, width) / max_long_side
        height, width = int(height / beta), int(width / beta)

    # 计算缩放后的尺寸
    h_bar = round_by_factor(height, factor)
    w_bar = round_by_factor(width, factor)

    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)
        w_bar = ceil_by_factor(width * beta, factor)

    return h_bar, w_bar

print(smart_resize(1920,3072))