# -*- coding: utf-8 -*-
# @Author: BugNotFound
# @Description: Delta Force 脚本 (适配 PaddleOCR 3.3.2 + AMD GPU ONNX)

import os
import sys
import re
import time
import ctypes

from window_capture import *
from region_selector import RegionSelector
from gui_monitor import MonitorWindow

import numpy
from paddleocr import PaddleOCR
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QThread, pyqtSignal
import pydirectinput
from colormath.color_objects import sRGBColor, LabColor
from colormath.color_diff import delta_e_cie2000
from colormath.color_conversions import convert_color

# 跳过联网检查，提升启动速度
os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"


def patch_asscalar(a):
    return a.item()


setattr(numpy, "asscalar", patch_asscalar)


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    if not is_admin():
        script = os.path.abspath(sys.argv[0])
        params = ' '.join([script] + sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if ret > 32: sys.exit(0)
        return False
    return True


def click_region_center(region: tuple, clicks=1, interval=0.1):
    left, top, right, bottom = region
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    center_x += int((os.urandom(1)[0] / 255 - 0.5) * 10)
    center_y += int((os.urandom(1)[0] / 255 - 0.5) * 10)
    pydirectinput.click(x=center_x, y=center_y, clicks=clicks, interval=interval, button=pydirectinput.LEFT)


def extract_and_merge_digits(s: str) -> str:
    return ''.join(re.findall(r'\d', s))


class ScriptThread(QThread):
    status_updated = pyqtSignal(str)
    timer_updated = pyqtSignal(str, str)
    ocr_updated = pyqtSignal(str, float)
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
        left, top, right, bottom = region
        # 确保坐标合法
        left, top = max(0, left), max(0, top)
        return frame[top:bottom, left:right]

    def verify_window(self) -> bool:
        frame = self.win_cap.capture()
        if frame is None or frame.size == 0: return False
        region = self.selector.get_region("verify_check")
        color_tmp = frame[((region[1] + region[3]) // 2), ((region[0] + region[2]) // 2)]
        center_color = convert_color(sRGBColor(color_tmp[2] / 255, color_tmp[1] / 255, color_tmp[0] / 255), LabColor)
        target_color = convert_color(sRGBColor(175 / 255, 109 / 255, 65 / 255), LabColor)
        return delta_e_cie2000(center_color, target_color) < 80

    def ocr_region(self, region):
        """适配 PaddleOCR 3.3.2 predict 接口"""
        frame = self.win_cap.capture()
        if frame is None or frame.size == 0: return ""
        roi = self.frame_cut(frame, region)

        try:
            # 3.x 版本推荐使用 predict
            # 且使用 ONNX 引擎时，内部会自动处理
            results = self.ocr.predict(roi)

            # 解析新版返回结构
            # 3.3.2 的返回通常是一个结果列表，每个结果包含 'rec_texts'
            texts = []
            for res in results:
                if hasattr(res, 'doc_res'):  # 文档模式
                    texts.extend(res.doc_res.get('rec_texts', []))
                elif isinstance(res, dict) and 'rec_texts' in res:  # 字典模式
                    texts.extend(res['rec_texts'])
                elif hasattr(res, 'rec_texts'):  # 对象模式
                    texts.extend(res.rec_texts)

            return "".join(texts) if texts else ""
        except Exception as e:
            print(f"OCR Error: {e}")
            return ""

    def run(self):
        try:
            self.status_updated.emit("初始化中...")
            time_region = self.selector.get_region("time")
            buy_region = self.selector.get_region("buy")
            verify_region = self.selector.get_region("verify")
            refresh_region = self.selector.get_region("refresh")
            money_region = self.selector.get_region("money")

            money_text = self.ocr_region(money_region)
            money = extract_and_merge_digits(money_text)
            self.status_updated.emit(f"初始三角币: {money}")
            pattern = re.compile(r'(\d+)\s*分\s*(\d+)\s*秒')

            self.status_updated.emit("监控中...")
            refreshed = False
            click_region_center(refresh_region)

            while self.is_running:
                while self.is_paused:
                    time.sleep(0.2)
                    continue

                res = self.ocr_region(time_region)
                # print(f"识别时间内容: {res}") # 调试用

                if "天" in res or "小时" in res:
                    click_region_center(refresh_region)
                    time.sleep(1)
                    continue

                match = pattern.search(res)
                if match:
                    minutes, seconds = int(match.group(1)), int(match.group(2))
                    self.timer_updated.emit(str(minutes), str(seconds))

                    if minutes == 0 and seconds == 3 and self.config['click_refresh_at_3s'] and not refreshed:
                        click_region_center(refresh_region)
                        refreshed = True

                    if minutes == 0 and seconds == 1:
                        self.status_updated.emit("时间到！执行购买...")
                        time.sleep(self.config['buy_click_delay'])
                        click_region_center(buy_region, interval=0)

                        buy_count = 0
                        while not self.verify_window() and buy_count < 5:
                            buy_count += 1
                            if buy_count <= 2:
                                time.sleep(self.config['buy_interval'])
                                click_region_center(buy_region, interval=0)

                        time.sleep(self.config['buy_to_verify_delay'])
                        click_region_center(verify_region, interval=self.config['verify_interval'])

                        verify_counter = 0
                        while self.verify_window():
                            verify_counter += 1
                            if verify_counter > 2: pydirectinput.click(1, 1, interval=0.1)
                            click_region_center(verify_region, interval=self.config['verify_interval'])

                        time.sleep(1.5)
                        if self.verify_window(): pydirectinput.press('esc')
                        click_region_center(refresh_region)

                        now_money = extract_and_merge_digits(self.ocr_region(money_region))
                        self.status_updated.emit(f"当前三角币: {now_money}")

                        if not self.config['continue_after_complete'] or now_money != money:
                            self.status_updated.emit("任务完成！")
                            self.task_completed.emit()
                            break
                        refreshed = False
                    else:
                        if minutes > 0 or seconds > 5: time.sleep(self.config['ocr_interval'])
                else:
                    # 如果没匹配到时间，稍微缩短等待再试
                    time.sleep(0.1)
        except Exception as e:
            self.status_updated.emit(f"运行错误: {str(e)}")

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def stop(self):
        self.is_running = False


def main():
    app = QApplication(sys.argv)
    selector = RegionSelector()
    selector.load_regions_from_file("regions_2k.json")
    win_cap = WindowCapture(max_buffer_len=2)

    # PaddleOCR 3.3.2 实例化
    # 启用 ONNX 以适配 AMD 显卡（需已安装 onnxruntime-directml）
    ocr = PaddleOCR(lang="ch", use_onnx=True)

    window = MonitorWindow()
    window.show()

    screen = app.primaryScreen().geometry()
    window.move(screen.x() + 10, screen.y() + screen.height() - window.height() - 30)
    window.add_log("程序已启动 (适配 AMD GPU/Paddle 3.3.2)")

    script_thread = None

    def on_start():
        nonlocal script_thread
        config = window.get_config()
        script_thread = ScriptThread(selector, win_cap, ocr, config)
        script_thread.status_updated.connect(window.update_status)
        script_thread.status_updated.connect(window.add_log)
        script_thread.timer_updated.connect(window.update_timer)
        script_thread.task_completed.connect(window.on_complete)
        script_thread.start()

    window.controller.start_requested.connect(on_start)
    window.controller.pause_requested.connect(lambda: script_thread.pause() if script_thread else None)
    window.controller.resume_requested.connect(lambda: script_thread.resume() if script_thread else None)
    window.controller.stop_requested.connect(
        lambda: (script_thread.stop(), script_thread.wait()) if script_thread else None)

    app.aboutToQuit.connect(lambda: (win_cap.stop(), (script_thread.stop() if script_thread else None)))
    sys.exit(app.exec())


if __name__ == "__main__":
    if is_admin():
        main()
    else:
        run_as_admin()