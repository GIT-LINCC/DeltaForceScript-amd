# -*- coding: utf-8 -*-
# @Author: BugNotFound
# @Date: 2025-10-04
# @FilePath: /DeltaForceScript/main_gui.py
# @Description: 带 PyQt6 GUI 的主程序

import os
import sys
# 解决 Intel OpenMP 库冲突导致的 DLL 初始化失败
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# 强制将 onnxruntime 提前加载，避免被其他库（如 PyQt）干扰环境
try:
    import onnxruntime as ort
    print(f"ONNX 加载成功，可用后端: {ort.get_available_providers()}")
except Exception as e:
    print(f"ONNX 预加载失败: {e}")
import re
import time
import ctypes
import cv2

from window_capture import *
from region_selector import RegionSelector
from gui_monitor import MonitorWindow

import numpy
from rapidocr_onnxruntime import RapidOCR
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QThread, pyqtSignal
import pydirectinput
from colormath.color_objects import sRGBColor, LabColor
from colormath.color_diff import delta_e_cie2000
from colormath.color_conversions import convert_color


def patch_asscalar(a):
    return a.item()


setattr(numpy, "asscalar", patch_asscalar)


def is_admin():
    """检查是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    """以管理员权限重新启动程序"""
    if not is_admin():
        print("正在请求管理员权限...")
        # 获取当前脚本路径
        script = os.path.abspath(sys.argv[0])
        params = ' '.join([script] + sys.argv[1:])

        # 使用 ShellExecute 以管理员权限运行
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )

        if ret > 32:  # 成功
            sys.exit(0)
        else:
            print("未获得管理员权限，继续以普通权限运行")
            return False
    return True


def click_region_center(region: tuple, clicks=1, interval=0.1):
    """点击区域的中心位置 - 使用多种方法尝试

    Args:
        region: (left, top, right, bottom) 格式的区域坐标
    """
    left, top, right, bottom = region
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2

    # print(f"准备点击位置: ({center_x}, {center_y})")
    # 在20个像素的范围内随机偏移，防止被检测
    center_x += int((os.urandom(1)[0] / 255 - 0.5) * 10)
    center_y += int((os.urandom(1)[0] / 255 - 0.5) * 10)

    pydirectinput.click(x=center_x, y=center_y, clicks=clicks, interval=interval, button=pydirectinput.LEFT)


def extract_and_merge_digits(s: str) -> str:
    """识别字符串中的所有数字并合并为一个新字符串"""
    return ''.join(re.findall(r'\d', s))


class ScriptThread(QThread):
    """脚本运行线程"""

    status_updated = pyqtSignal(str)
    timer_updated = pyqtSignal(str, str)
    ocr_updated = pyqtSignal(str, float)
    click_performed = pyqtSignal()
    task_completed = pyqtSignal()

    def __init__(self, selector: RegionSelector, win_cap: WindowCapture, ocr, config):
        super().__init__()
        self.selector = selector
        self.win_cap = win_cap
        self.ocr = ocr
        self.config = config
        self.is_running = True
        self.is_paused = False

    def frame_cut(self, frame, region):
        """裁剪图像区域"""
        left, top, right, bottom = region
        return frame[top:bottom, left:right]

    # def verify_window(self) -> bool:
    #     """检查确认按钮区域的颜色是否变化"""
    #     frame = self.win_cap.capture()
    #     while frame is None or frame.size == 0: frame = self.win_cap.capture()
    #     region = self.selector.get_region("verify_check")
    #     # 获取区域中心颜色
    #     color_tmp = frame[((region[1] + region[3]) // 2), ((region[0] + region[2]) // 2)]
    #     center_color = convert_color(
    #         sRGBColor(color_tmp[2], color_tmp[1], color_tmp[0]),  # BGR to sRGB
    #         LabColor
    #     )
    #     # 预设的确认按钮中心颜色 (BGR)
    #     target_color = convert_color(
    #         sRGBColor(175, 109, 65),  # BGR：适用于金色砖皮
    #         LabColor
    #     )
    #     # 计算颜色差异
    #     delta_e = delta_e_cie2000(center_color, target_color)
    #     # 色差小说明显示了确认窗口
    #     self.status_updated.emit(f"颜色：{color_tmp[2], color_tmp[1], color_tmp[0]}")
    #     self.status_updated.emit(f"色差: {delta_e}")
    #     if delta_e < 80:
    #         return True
    #     return False

    def verify_window(self) -> bool:
        """检查确认按钮区域的颜色是否变化 (优化版，不使用 colormath)"""
        frame = self.win_cap.capture()
        if frame is None or frame.size == 0:
            return False

        region = self.selector.get_region("verify_check")
        # 确保坐标合法
        l, t, r, b = region
        if r <= l or b <= t: return False

        # 获取区域中心点的颜色 (BGR)
        center_x, center_y = (l + r) // 2, (t + b) // 2
        # 注意：OpenCV 坐标是 [y, x]
        color_bgr = frame[center_y, center_x]

        # 预设的确认按钮目标颜色 (这里根据你的日志 175, 109, 65 调整)
        # 假设目标 BGR 是 [65, 109, 175]
        target_bgr = numpy.array([65, 109, 175])
        current_bgr = numpy.array(color_bgr)

        # 计算欧氏距离 (取代 delta_e)
        distance = numpy.linalg.norm(current_bgr - target_bgr)

        # self.status_updated.emit(f"颜色距离: {distance:.2f}") # 调试用

        # 距离越小颜色越接近，通常距离小于 30 就认为匹配成功
        if distance < 50:
            return True
        return False

    # def ocr_region(self, region):
    #     """OCR 识别"""
    #     frame = self.win_cap.capture()
    #     # while frame is None or frame.size == 0: frame = self.win_cap.capture()
    #     if frame is None or frame.size == 0: return ""
    #     roi = self.frame_cut(frame, region)
    #     res = self.ocr.ocr(roi)
    #     if not res or not res[0]['rec_texts']:
    #         return ""
    #     return res[0]['rec_texts'][0]

    def ocr_region(self, region_name, region):
        """OCR 识别 (适配 RapidOCR + 防错处理)"""
        frame = self.win_cap.capture()
        if frame is None or frame.size == 0:
            return ""

        # 确保裁剪区域合法
        left, top, right, bottom = region
        if right <= left or bottom <= top:
            return ""

        roi = frame[top:bottom, left:right]
        if roi.size == 0:
            return ""
        # --- 策略分流 ---
        if region_name == "money":
            # 三角币识别：直接识别，不处理（或者只做简单的灰度）
            input_img = roi
        else:
            # 时间识别：使用自适应处理，不要用固定 150 阈值
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            # 使用自适应二值化 (Adaptive Thresholding) 应对光影变化
            binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY, 11, 2)
            # 只放大 1.5 倍，避免锯齿严重
            upscaled = cv2.resize(binary, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
            input_img = cv2.cvtColor(upscaled, cv2.COLOR_GRAY2BGR)

        try:
            result, _ = self.ocr(input_img)
            if result:
                full_text = "".join([line[1] for line in result])
                return full_text
        except:
            pass
        return ""
    # 调用 RapidOCR
        # try:
        #     result, _ = self.ocr(roi)
        #     if result and len(result) > 0:
        #         # result 格式: [[box, text, score], ...]
        #         return str(result[0][1])  # 返回识别到的第一个文本
        # except Exception as e:
        #     print(f"OCR 内部错误: {e}")
        #
        # return ""


    def run(self):
        """运行脚本"""
        try:
            self.status_updated.emit("初始化中...")

            time_region = self.selector.get_region("time")
            # 初始化记录变量（放在 run 函数开始处）
            self.last_ui_min = ""
            self.last_ui_sec = ""
            buy_region = self.selector.get_region("buy")
            verify_region = self.selector.get_region("verify")
            refresh_region = self.selector.get_region("refresh")
            money_region = self.selector.get_region("money")

            # money = self.ocr_region(money_region)
            money = self.ocr_region("money", money_region)
            money = extract_and_merge_digits(money)
            self.status_updated.emit(f"初始三角币: {money}")
            pattern = re.compile(r'(\d+)\s*分\s*(\d+)\s*秒')

            # --- 增加：初始化时间校验变量 ---
            last_total_seconds = 9999
            # ---------------------------

            self.status_updated.emit("监控中...")
            refreshed = False  # 标记是否刚刚点击过刷新
            click_region_center(refresh_region)
            while self.is_running:
                # 暂停时等待
                while self.is_paused: time.sleep(0.2); continue

                # 截图并OCR识别时间
                res = self.ocr_region("time", time_region)
                # print(f"DEBUG - 时间区域识别结果: '{res}'")
                # if "天" in res or "小时" in res: click_region_center(refresh_region); continue
                # match = pattern.search(res)
                # if match:
                #     minutes = int(match.group(1))
                #     seconds = int(match.group(2))
                #     # 更新时间显示
                #     self.timer_updated.emit(str(minutes), str(seconds))


                # --- 增强处理开始 ---
                if not res:
                    time.sleep(self.config['ocr_interval'])
                    continue

                # 预处理字符串：去掉空格，统一替换常见误识别字符
                clean_res = res.replace(" ", "").replace("份", "分").replace("b", "6")

                if "天" in clean_res or "小时" in clean_res:
                    click_region_center(refresh_region)
                    # 刷新后重置校验值，允许时间变大
                    last_total_seconds = 9999
                    refreshed = True
                    continue

                # 更加宽松的正则匹配：只提取数字，不强制要求中间有“分”或“秒”
                # 原逻辑：match = pattern.search(res)
                # 建议逻辑：直接尝试提取前两个连续数字序列
                digits = re.findall(r'\d+', clean_res)

                if len(digits) >= 2:
                    minutes = int(digits[0])
                    seconds = int(digits[1])
                    # 过滤掉不合理的数值（比如识别到了其他地方的数字）
                    if minutes > 60 or seconds > 60:
                        continue
                    # --- 增加：逻辑过滤校验 ---
                    current_total_seconds = minutes * 60 + seconds

                    # 如果当前时间比上次大，且不是刚刷新（且在1小时内），判定为误读
                    if current_total_seconds > last_total_seconds and current_total_seconds < 3600:
                        if not refreshed:
                            # self.status_updated.emit(f"检测到时间跳变: {last_total_seconds} -> {current_total_seconds}，已忽略")
                            continue
                    # 校验通过，更新最后一次记录的时间
                    last_total_seconds = current_total_seconds
                    refreshed = False  # 已经成功识别一次，重置刷新状态
                    # -------------------------

                    # 更新时间显示
                    current_min = str(minutes)
                    current_sec = str(seconds)

                    # 只有当时间数字真正改变时，才触发 UI 更新
                    if current_min != self.last_ui_min or current_sec != self.last_ui_sec:
                        self.timer_updated.emit(current_min, current_sec)
                        self.last_ui_min = current_min
                        self.last_ui_sec = current_sec
                    # --- 增强处理结束 ---

                    # 剩余时间到 0:03 时点击刷新（如果启用）
                    if minutes == 0 and seconds == 3 and self.config['click_refresh_at_3s'] and not refreshed:
                        self.status_updated.emit("🔄 点击刷新...")
                        click_region_center(refresh_region)
                        refreshed = True
                    # 剩余时间到 0:01 时执行点击
                    if minutes == 0 and seconds == 1:
                        self.status_updated.emit("准备点击...")
                        time.sleep(self.config['buy_click_delay'])
                        # 点击购买按钮
                        click_region_center(buy_region, interval=0)
                        # 校验点击是否成功（可能造成延迟）
                        buy_count = 0
                        while not self.verify_window() and buy_count < 5:
                            buy_count += 1
                            if buy_count <= 2:
                                time.sleep(self.config['buy_interval'])
                                click_region_center(buy_region, interval=0)
                        time.sleep(self.config['buy_to_verify_delay'])
                        # 点击确认按钮
                        click_region_center(verify_region, interval=self.config['verify_interval'])
                        self.status_updated.emit("点击确认按钮...")
                        # 校验点到了确认
                        verify_counter = 0
                        while self.verify_window():
                            verify_counter += 1
                            if verify_counter > 2:
                                pydirectinput.click(1, 1, interval=0.1)
                            click_region_center(verify_region, interval=self.config['verify_interval'])

                        self.status_updated.emit("等待刷新...")
                        time.sleep(1.5)
                        if self.verify_window(): pydirectinput.press('esc')
                        click_region_center(refresh_region)
                        # 成功抢购或结束后，重置校验
                        last_total_seconds = 9999
                        # 检查三角币是否变化
                        now_money = self.ocr_region(money_region)
                        now_money = extract_and_merge_digits(now_money)
                        self.status_updated.emit(f"当前三角币: {now_money}")
                        self.config['continue_after_complete'] &= (now_money == money)
                        # 根据配置决定是否继续
                        if not self.config['continue_after_complete']:
                            self.status_updated.emit("任务完成！")
                            self.task_completed.emit()
                            break
                        else:
                            refreshed = False
                            self.status_updated.emit("继续监控中...")
                    else:
                        if minutes > 0 or seconds > 5:
                            time.sleep(self.config['ocr_interval'])
                else:
                    time.sleep(self.config['ocr_interval'])
        except Exception as e:
            self.status_updated.emit(f"错误: {str(e)}")
            print(f"脚本运行错误: {e}")

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def stop(self):
        self.is_running = False


def main():
    """主函数"""
    app = QApplication(sys.argv)
    selector = RegionSelector()
    # selector.load_regions_from_file("regions_2k.json")
    selector.load_regions_from_file("regions_config.json")
    win_cap = WindowCapture(max_buffer_len=2)

    # 初始化 OCR

    ocr = RapidOCR(
        # use_doc_orientation_classify=False,
        # use_doc_unwarping=False,
        # use_textline_orientation=False,
        # text_detection_model_dir="models/PP-OCRv5_server_det_infer",
        # text_recognition_model_dir="models/PP-OCRv5_server_rec_infer",
        # # use_tensorrt=True,
        # device='gpu:0'
        det_score_mode='fast',  # 快速模式
        binarize=True  # 内部开启二值化
    )

    window = MonitorWindow()
    window.show()
    # 移动到屏幕右下角
    screen = app.primaryScreen().geometry()
    win_h = window.height()
    x = screen.x() + 10
    y = screen.y() + screen.height() - win_h - 30
    window.move(x, y)
    window.add_log("程序已启动")
    window.add_log("点击 [开始] 按钮启动监控")
    script_thread = None

    def on_start():
        nonlocal script_thread
        window.add_log("正在启动监控线程...")

        # 获取当前配置
        config = window.get_config()
        window.add_log(f"配置: 购买延迟={config['buy_click_delay']}秒")

        script_thread = ScriptThread(selector, win_cap, ocr, config)

        script_thread.status_updated.connect(lambda s: window.update_status(s))
        script_thread.status_updated.connect(lambda s: window.add_log(s))
        script_thread.timer_updated.connect(lambda m, s: window.update_timer(m, s))
        script_thread.task_completed.connect(lambda: window.on_complete())

        script_thread.start()

    def on_pause():
        if script_thread:
            script_thread.pause()

    def on_resume():
        if script_thread:
            script_thread.resume()

    def on_stop():
        if script_thread:
            script_thread.stop()
            script_thread.wait()

    window.controller.start_requested.connect(on_start)
    window.controller.pause_requested.connect(on_pause)
    window.controller.resume_requested.connect(on_resume)
    window.controller.stop_requested.connect(on_stop)

    def cleanup():
        if script_thread and script_thread.isRunning():
            script_thread.stop()
            script_thread.wait()
        win_cap.stop()

    app.aboutToQuit.connect(cleanup)

    sys.exit(app.exec())


if __name__ == "__main__":
    # 检查并请求管理员权限
    if not is_admin():
        print("检测到程序未以管理员权限运行")
        run_as_admin()
    else:
        print("Delta Force 自动购买脚本 - PyQt6 GUI版本 (管理员模式)")
        main()
