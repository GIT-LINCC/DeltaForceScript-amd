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

import bettercam
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
import win32gui
import win32api
import win32con
# Windows 鼠标常量
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

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


def fast_click(hwnd, x, y):
    """
    使用 Win32 消息队列直接发送点击指令（不移动物理鼠标）
    x, y 必须是相对于窗口左上角的坐标
    """
    lparam = win32api.MAKELONG(int(x), int(y))
    # 发送按下消息
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    # 模拟按下和弹起之间的微小间隔（可选，有些游戏需要）
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)

def get_relative_pos(hwnd, screen_x, screen_y):
    """将屏幕绝对坐标转换为相对于窗口客户区的坐标"""
    point = win32gui.ScreenToClient(hwnd, (screen_x, screen_y))
    return point[0], point[1]


def win32_hardware_click(x, y):
    """
    通过 Windows API 模拟硬件级点击
    解决“有音效无实际点击”的问题
    """
    # 1. 坐标转换：屏幕像素转为 Windows 绝对坐标 (0-65535)
    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)
    nx = int(x * 65535 / (w - 1))
    ny = int(y * 65535 / (h - 1))

    # 2. 模拟真实动作序列：移动 -> 按下 -> 延迟 -> 弹起
    # 移动
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE, nx, ny, 0, 0)
    # 按下
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN, nx, ny, 0, 0)
    # 这里的微小延迟（约10ms）非常重要，防止点击太快被引擎过滤
    time.sleep(0.01)
    # 弹起
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP, nx, ny, 0, 0)


def click_region_center(region: tuple):
    """适配新 API 的区域点击"""
    left, top, right, bottom = region
    cx = (left + right) // 2
    cy = (top + bottom) // 2

    # 加入微小的随机偏移，增加防封安全性
    cx += int((os.urandom(1)[0] / 255 - 0.5) * 6)
    cy += int((os.urandom(1)[0] / 255 - 0.5) * 6)

    win32_hardware_click(cx, cy)
# def click_region_center(region: tuple, clicks=1, interval=0.1):
#     """点击区域的中心位置 - 使用多种方法尝试
#
#     Args:
#         region: (left, top, right, bottom) 格式的区域坐标
#     """
#     left, top, right, bottom = region
#     center_x = (left + right) // 2
#     center_y = (top + bottom) // 2
#
#     # print(f"准备点击位置: ({center_x}, {center_y})")
#     # 在20个像素的范围内随机偏移，防止被检测
#     center_x += int((os.urandom(1)[0] / 255 - 0.5) * 10)
#     center_y += int((os.urandom(1)[0] / 255 - 0.5) * 10)
#
#     pydirectinput.click(x=center_x, y=center_y, clicks=clicks, interval=interval, button=pydirectinput.LEFT)


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
        # --- 新增：寻找窗口句柄 ---
        # 请确保窗口标题正确，例如 "Delta Force" 或 "三角洲行动"
        self.hwnd = win32gui.FindWindow(None, "三角洲行动  ")
        if not self.hwnd:
            print("警告：未找到游戏窗口句柄！")

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
        """整合 Windows API 的抢购逻辑"""
        try:
            self.status_updated.emit("监控中 (硬件模拟模式)...")

            buy_region = self.selector.get_region("buy")
            verify_region = self.selector.get_region("verify")
            verify_check = self.selector.get_region("verify_check")
            refresh_region = self.selector.get_region("refresh")
            target_bgr = numpy.array([32, 29, 20])

            while self.is_running:
                if self.is_paused:
                    time.sleep(0.1)
                    continue

                frame = self.win_cap.capture()
                if frame is None: continue

                # 获取购买按钮中心
                l, t, r, b = buy_region
                cx, cy = (l + r) // 2, (t + b) // 2
                current_bgr = frame[cy, cx]

                # 颜色匹配判断
                if numpy.linalg.norm(current_bgr - target_bgr) < 30:
                    # --- 执行硬件级点击 ---
                    win32_hardware_click(cx, cy)
                    self.status_updated.emit("触发购买！")

                    # --- 快速检测确认弹窗 ---
                    start_v = time.time()
                    while time.time() - start_v < 2.0:
                        frame = self.win_cap.capture()
                        if frame is None: continue

                        # 检测确认区域
                        # l2, t2, r2, b2 = verify_check
                        l2, t2, r2, b2 = verify_region
                        cx2, cy2 = (l2 + r2) // 2, (t2 + b2) // 2

                        if numpy.linalg.norm(frame[cy2, cx2] - target_bgr) < 50:
                            # 再次执行硬件级点击
                            win32_hardware_click(cx2, cy2)
                            time.sleep(0.25)
                            win32_hardware_click(cx2, cy2)
                            time.sleep(0.1)
                            win32_hardware_click(cx2, cy2)
                            time.sleep(0.1)
                            win32_hardware_click(cx2, cy2)
                            time.sleep(0.1)
                            win32_hardware_click(cx2, cy2)
                            time.sleep(0.1)
                            win32_hardware_click(cx2, cy2)
                            win32_hardware_click(cx2, cy2)
                            self.status_updated.emit("确认成功")
                            time.sleep(1.0)
                            l3, t3, r3, b3 = refresh_region
                            cx3, cy3 = (l3 + r3) // 2, (t3 + b3) // 2
                            win32_hardware_click(cx3, cy3)

                time.sleep(0.001)

        except Exception as e:
            self.status_updated.emit(f"错误: {str(e)}")

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
