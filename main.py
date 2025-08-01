import warnings
import os
import sys
import gc

from utils.system_utils import (
    check_dependencies,
    set_dig_tool_instance,
    send_click,
    check_display_scale,
    measure_system_latency,
)

check_dependencies()

try:
    import ctypes

    PROCESS_PER_MONITOR_DPI_AWARE = 2
    ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
except:
    pass

import cv2
import numpy as np
import tkinter as tk
from tkinter import Label, Frame, TclError
try:
    from tkinterdnd2 import TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False
import threading
import time
from PIL import Image, ImageTk
import keyboard
import queue

from interface.components import GameOverlay, AutoWalkOverlay
from interface.main_window import MainWindow
from interface.settings import SettingsManager
from interface.custom_pattern_window import CustomPatternWindow
from utils.debug_logger import logger, save_debug_screenshot, log_click_debug
from utils.screen_capture import ScreenCapture
from core.detection import (
    find_line_position,
    VelocityCalculator,
    get_hsv_bounds,
    calculate_velocity_based_sweet_spot_width,
    detect_by_otsu_with_area_filter,
    detect_by_otsu_adaptive_area,
    detect_by_color_picker,
    rgb_to_hsv_single,
)
from core.automation import AutomationManager, perform_click_action
from core.notifications import DiscordNotifier

warnings.filterwarnings("ignore")
check_display_scale()


class DigTool:
    def __init__(self):
        logger.enable_logging_for_startup(30)

        if DND_AVAILABLE:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()
        self.root.title("Dig Tool")

        self.root.wm_iconbitmap(
            os.path.join(sys._MEIPASS, "assets/icon.ico")
            if hasattr(sys, "_MEIPASS")
            else "assets/icon.ico"
        )

        self.base_height = 570
        self.width = 450
        self.root.geometry(f"{self.width}x{self.base_height}")
        self.root.minsize(self.width, self.base_height)

        self.param_vars = {}
        self.keybind_vars = {}
        self.last_known_good_params = {}
        self.window_positions = {}

        self.game_area = None
        self.cursor_position = None

        self.settings_manager = SettingsManager(self)

        self._initialize_default_param_vars()

        self.automation_manager = AutomationManager(self)
        self.discord_notifier = DiscordNotifier()

        self.settings_manager.load_all_settings()

        self.automation_manager.auto_load_patterns()

        self.main_window = MainWindow(self)
        self.custom_pattern_window = None

        self.root.after_idle(lambda: self.settings_manager.apply_loaded_parameters())

        self.root.after_idle(lambda: self._check_and_enable_buttons())

        set_dig_tool_instance(self)
        self.root.after(500, self.perform_initial_latency_measurement)
        self.running = False
        self.preview_active = True
        self.overlay = None
        self.overlay_enabled = False
        self.autowalk_overlay = None
        self.autowalk_overlay_enabled = False
        self.cam = ScreenCapture()
        self.region_key = "main_game"
        self.click_count = 0
        self.dig_count = 0
        self.click_lock = threading.Lock()
        self.velocity_calculator = VelocityCalculator()
        self.blind_until = 0
        self.frames_since_last_zone_detection = 0
        self.smoothed_zone_x = None
        self.smoothed_zone_w = None
        self.is_color_locked = False
        self.locked_color_hsv = None
        self.locked_color_hex = None
        self.is_low_sat_lock = False
        self.preview_window = None
        self.debug_window = None
        self.preview_label = None
        self.debug_label = None
        self.color_swatch_label = None
        self.detection_info_label = None
        self.velocity_info_label = None
        self.main_loop_thread = None
        self.hotkey_thread = None
        self.results_queue = queue.Queue(maxsize=1)
        self.debug_dir = "debug_clicks"
        self.debug_log_path = os.path.join(self.debug_dir, "click_log.txt")

        self._memory_cleanup_counter = 0
        self._cached_kernel = None
        self._cached_kernel_size = 0

        self.last_milestone_notification = 0

        self.target_engaged = False
        self.line_moving_history = []
        self.base_line_movement_check_frames = 30
        self.min_movement_threshold = 50

        self._kernel = np.ones((5, 15), np.uint8)
        self._hsv_lower_bound_cache = None
        self._hsv_upper_bound_cache = None
        self._last_hsv_color = None
        self._last_is_low_sat = None

        self._current_time_cache = 0
        self._current_time_ms_cache = 0
        self._last_time_update = 0

        self._click_thread_pool = []
        self._max_click_threads = 3

        # Benchmarking
        self.report_interval = 1
        self.frame_times = []
        self.last_report_time = time.time()
        self.last_frame_time = time.perf_counter()
        self.benchmark_fps = 0

        self.main_window.create_ui()

        if hasattr(self, "screen_grabber"):
            pass

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(50, self.update_gui_from_queue)

    def open_custom_pattern_manager(self):
        if self.custom_pattern_window is None:
            self.custom_pattern_window = CustomPatternWindow(
                self.root, self.automation_manager
            )
        self.custom_pattern_window.show_window()

    def update_walk_pattern_dropdown(self):
        if hasattr(self.main_window, "walk_pattern_combo"):
            current_value = self.main_window.walk_pattern_combo.get()
            pattern_info = self.automation_manager.get_pattern_list()
            pattern_names = list(pattern_info.keys())

            self.main_window.walk_pattern_combo["values"] = pattern_names

            if current_value in pattern_names:
                self.main_window.walk_pattern_combo.set(current_value)
            elif pattern_names:
                self.main_window.walk_pattern_combo.set(pattern_names[0])

        if self.autowalk_overlay and self.autowalk_overlay.visible:
            self.autowalk_overlay.update_path_visualization()

        if self.autowalk_overlay and self.autowalk_overlay.visible:
            self.autowalk_overlay.update_path_visualization()

    def on_walk_pattern_changed(self, *args):
        if (
            hasattr(self, "autowalk_overlay")
            and self.autowalk_overlay
            and self.autowalk_overlay.visible
        ):
            self.autowalk_overlay.update_pattern_name()
            self.autowalk_overlay.update_path_visualization()

        self.root.after_idle(
            lambda: self.settings_manager.auto_save_setting("coordinates")
        )

    def check_line_movement(self, line_pos, target_fps):
        line_movement_check_frames = max(
            int(self.base_line_movement_check_frames * (target_fps / 120.0)), 10
        )

        self.line_moving_history.append(line_pos)

        if len(self.line_moving_history) > line_movement_check_frames:
            self.line_moving_history.pop(0)

        if len(self.line_moving_history) < 10:
            return False

        valid_positions = [pos for pos in self.line_moving_history if pos != -1]

        if len(valid_positions) < 5:
            return False

        min_pos = min(valid_positions)
        max_pos = max(valid_positions)
        movement_range = max_pos - min_pos

        return movement_range >= self.min_movement_threshold

    def check_target_engagement(self, line_pos, target_fps):
        line_detected = line_pos != -1
        line_moving = self.check_line_movement(line_pos, target_fps)

        return line_detected and line_moving

    def measure_system_latency(self):
        if hasattr(self, "_measured_latency") and hasattr(
            self, "_latency_measurement_time"
        ):
            if time.time() - self._latency_measurement_time < 30:
                return self._measured_latency
              
        game_area = getattr(self, "game_area", None)
        self._measured_latency = measure_system_latency(game_area, self.cam)

        self._latency_measurement_time = time.time()
        return self._measured_latency

    def force_latency_remeasurement(self):
        if hasattr(self, "_latency_measurement_time"):
            self._latency_measurement_time = 0

        new_latency = self.measure_system_latency()
        self._cached_latency = new_latency
        return new_latency

    def ensure_debug_dir(self):
        if self.get_param("debug_clicks_enabled") and not os.path.exists(
            self.debug_dir
        ):
            os.makedirs(self.debug_dir)

    def on_closing(self):
        try:
            self.settings_manager.save_all_settings()
            logger.info("Settings saved on application close")
        except Exception as e:
            logger.error(f"Error saving settings on close: {e}")

        if self.running:
            try:
                webhook_url = self.param_vars.get("webhook_url", tk.StringVar()).get()
                user_id = self.param_vars.get("user_id", tk.StringVar()).get()
                if webhook_url:
                    self.discord_notifier.set_webhook_url(webhook_url)
                    threading.Thread(target=self.discord_notifier.send_shutdown_notification, args=(user_id if user_id else None), daemon=True).start()
            except:
                pass

        self.preview_active = False
        self.running = False
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.update_status("Shutting down...")
        self.root.after(100, self._check_shutdown)

    def _check_shutdown(self):
        hotkey_alive = self.hotkey_thread and self.hotkey_thread.is_alive()
        main_loop_alive = self.main_loop_thread and self.main_loop_thread.is_alive()
        if hotkey_alive or main_loop_alive:
            self.root.after(100, self._check_shutdown)
        else:
            self._perform_final_cleanup()

    def _perform_final_cleanup(self):
        # refactor
        if self.overlay:
            self.overlay.destroy_overlay()
            self.overlay = None
        if self.autowalk_overlay:
            self.autowalk_overlay.destroy_overlay()
            self.autowalk_overlay = None

        if hasattr(self, "automation_manager") and self.automation_manager:
            self.automation_manager.cleanup()
            del self.automation_manager
            self.automation_manager = None

        try:
            keyboard.unhook_all()
        except:
            pass

        try:
          self.cam.close()
        except:
            pass

        for _ in range(3):
            gc.collect()

        try:
            import comtypes
            comtypes.CoUninitialize()
        except:
            pass

        self.root.after(100, self._force_exit)

    def _force_exit(self):
        # refactor
        try:
            self.root.destroy()
        except:
            pass

        import threading
        import time

        def delayed_exit():
            time.sleep(0.2)
            import os

            os._exit(0)

        exit_thread = threading.Thread(target=delayed_exit, daemon=True)
        exit_thread.start()

    def validate_keybind(self, key_name, key_value):
        if not key_value or key_value.strip() == "":
            return False, "Keybind cannot be empty"

        invalid_chars = [" ", "\t", "\n", "\r"]
        if any(char in key_value for char in invalid_chars):
            return False, "Keybind cannot contain spaces or whitespace"

        try:
            keyboard.normalize_name(key_value)
            return True, "Valid keybind"
        except Exception as e:
            return False, f"Invalid key name: {e}"

    def start_threads(self):
        if self.hotkey_thread is None or not self.hotkey_thread.is_alive():
            self.hotkey_thread = threading.Thread(
                target=self.hotkey_listener, daemon=True
            )
            self.hotkey_thread.start()
        if self.main_loop_thread is None or not self.main_loop_thread.is_alive():
            self.main_loop_thread = threading.Thread(
                target=self.run_main_loop, daemon=True
            )
            self.main_loop_thread.start()

    def hotkey_listener(self):
        logger.info("Hotkey listener thread started")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.root.after(0, self.apply_keybinds)
                logger.debug(f"Keybind application scheduled (attempt {attempt + 1})")
                break
            except Exception as e:
                logger.error(
                    f"Failed to schedule keybind application (attempt {attempt + 1}): {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(1)

        while self.preview_active:
            time.sleep(0.5)

        logger.info("Hotkey listener thread ended")

    def apply_keybinds(self):
        logger.info("Applying keybinds...")

        time.sleep(0.1)

        try:
            keyboard.unhook_all()
            logger.debug("Previous hotkeys unhooked")
        except Exception as e:
            logger.warning(f"Warning: Could not unhook previous hotkeys: {e}")

        if not self.keybind_vars:
            logger.error("Error: Keybind variables not initialized")
            self.update_status("Error: Keybind variables not initialized")
            return False

        try:
            keybinds_applied = 0

            for key_name, var in self.keybind_vars.items():
                key_value = var.get()
                if not key_value:
                    logger.warning(f"Warning: Empty keybind for {key_name}")
                    continue

                is_valid, msg = self.validate_keybind(key_name, key_value)
                if not is_valid:
                    logger.error(
                        f"Error: Invalid keybind for {key_name}: {key_value} - {msg}"
                    )
                    continue

                logger.debug(f"  Applying {key_name}: {key_value}")

                if key_name == "toggle_bot":
                    keyboard.add_hotkey(key_value, self.toggle_detection)
                elif key_name == "toggle_gui":
                    keyboard.add_hotkey(key_value, self.toggle_gui)
                elif key_name == "toggle_overlay":
                    keyboard.add_hotkey(key_value, self.toggle_overlay)
                elif key_name == "toggle_autowalk_overlay":
                    keyboard.add_hotkey(key_value, self.toggle_autowalk_overlay)
                else:
                    logger.warning(f"Warning: Unknown keybind {key_name}")
                    continue

                keybinds_applied += 1

            self.update_main_button_text()
            success_msg = f"Successfully applied {keybinds_applied} keybinds"
            self.update_status(success_msg)
            logger.info(success_msg)

            self.root.after_idle(
                lambda: self.settings_manager.auto_save_setting("keybinds")
            )

            return True

        except Exception as e:
            error_msg = f"Error applying keybinds: {e}"
            self.update_status(error_msg)
            logger.error(error_msg)
            self.update_main_button_text()
            return False

    def toggle_gui(self):
        self.root.after(0, self._toggle_gui_thread_safe)

    def _toggle_gui_thread_safe(self):
        if self.root.winfo_exists():
            if self.root.state() == "normal":
                self.root.withdraw()
            else:
                self.root.deiconify()
                self.root.lift()

    def toggle_overlay(self):
        if not self.root.winfo_exists():
            return
        self.root.after_idle(self._toggle_overlay_thread_safe)

    def _toggle_overlay_thread_safe(self):
        try:
            if not self.overlay_enabled:
                if not self.game_area:
                    self.update_status("Select game area first")
                    return
                self.overlay = GameOverlay(self)
                self.overlay.create_overlay()
                self.overlay_enabled = True
                logger.debug("Overlay enabled")
            else:
                if self.overlay:
                    self.overlay.destroy_overlay()
                self.overlay = None
                self.overlay_enabled = False
                logger.debug("Overlay disabled")
            self.update_main_button_text()
        except Exception as e:
            logger.error(f"Error toggling overlay: {e}")
            self.overlay_enabled = False
            self.overlay = None

    def toggle_autowalk_overlay(self):
        self.root.after_idle(self._toggle_autowalk_overlay_thread_safe)

    def _toggle_autowalk_overlay_thread_safe(self):
        try:
            if not self.autowalk_overlay_enabled:
                if not self.get_param("auto_walk_enabled"):
                    self.update_status(
                        "Auto Walk overlay requires Auto Walk to be enabled"
                    )
                    return

                self.autowalk_overlay = AutoWalkOverlay(self)
                self.autowalk_overlay.create_overlay()
                self.autowalk_overlay_enabled = True
                logger.debug("Auto Walk overlay enabled")
            else:
                if self.autowalk_overlay:
                    self.autowalk_overlay.destroy_overlay()
                self.autowalk_overlay = None
                self.autowalk_overlay_enabled = False
                logger.debug("Auto Walk overlay disabled")
        except Exception as e:
            logger.error(f"Error toggling auto walk overlay: {e}")

    def get_param(self, key):
        if key == "system_latency":
            return self.get_cached_system_latency()
        
        if key in self.param_vars:
            try:
                value = self.param_vars[key].get()
                if isinstance(value, str) and value.strip() == "":
                    default_value = self.settings_manager.get_default_value(key)
                    self.param_vars[key].set(default_value)
                    return default_value
                return value
            except:
                default_value = self.settings_manager.get_default_value(key)
                if default_value is not None:
                    self.param_vars[key].set(default_value)
                    return default_value
        return getattr(self, key, None)

    def set_param(self, key, value):
        if key in self.param_vars:
            try:
                if isinstance(value, str) and value.strip() == "":
                    default_value = self.settings_manager.get_default_value(key)
                    if default_value is not None:
                        self.param_vars[key].set(default_value)
                        setattr(self, key, default_value)
                        return

                if hasattr(self.settings_manager, "validate_param_value"):
                    if not self.settings_manager.validate_param_value(key, value):
                        default_value = self.settings_manager.get_default_value(key)
                        if default_value is not None:
                            self.param_vars[key].set(default_value)
                            setattr(self, key, default_value)
                            return

                self.param_vars[key].set(value)
            except:
                default_value = self.settings_manager.get_default_value(key)
                if default_value is not None:
                    self.param_vars[key].set(default_value)
                    setattr(self, key, default_value)
                    return

        setattr(self, key, value)

    def update_main_button_text(self):
        if not self.root.winfo_exists():
            return
        try:
            current_state = "Stop" if self.running else "Start"
            self.start_stop_btn.config(
                text=f"{current_state} ({self.keybind_vars['toggle_bot'].get().upper()})"
            )
            self.toggle_gui_btn.config(
                text=f"Show/Hide ({self.keybind_vars['toggle_gui'].get().upper()})"
            )
            overlay_status = "ON" if self.overlay_enabled else "OFF"
            self.overlay_btn.config(
                text=f"Overlay: {overlay_status} ({self.keybind_vars['toggle_overlay'].get().upper()})"
            )
        except (TclError, AttributeError):
            pass

    def resize_for_content(self):
        self.root.update_idletasks()
        open_pane = next((p for p in self.accordion.panes if p.is_open.get()), None)
        content_height = open_pane.sub_frame.winfo_reqheight() if open_pane else 0
        new_height = self.base_height + content_height + (10 if open_pane else 0)
        self.root.geometry(f"{self.width}x{new_height}")

    def update_area_info(self):
        if not hasattr(self, "area_info_label"):
            return
        if self.game_area:
            x1, y1, x2, y2 = self.game_area
            width, height = x2 - x1, y2 - y1
            area_text = f"Game Area: {width}x{height} at ({x1}, {y1})"
        else:
            area_text = "Game Area: Not set"
        self.area_info_label.config(text=area_text)

    def update_sell_info(self):
        if not hasattr(self, "sell_info_label"):
            return
        if self.automation_manager.sell_button_position:
            x, y = self.automation_manager.sell_button_position
            sell_text = f"Sell Button: Set at ({x}, {y})"
        else:
            sell_text = "Sell Button: Not set"
        self.sell_info_label.config(text=sell_text)

    def update_cursor_info(self):
        if not hasattr(self, "cursor_info_label"):
            return
        if hasattr(self, "cursor_position") and self.cursor_position:
            x, y = self.cursor_position
            cursor_text = f"Cursor Position: Set at ({x}, {y})"
        else:
            cursor_text = "Cursor Position: Not set"
        self.cursor_info_label.config(text=cursor_text)

    def start_area_selection(self):
        self.root.iconify()
        self.selection_overlay = tk.Toplevel()
        self.selection_overlay.attributes(
            "-fullscreen", True, "-alpha", 0.2, "-topmost", True
        )
        self.selection_overlay.configure(bg="blue", cursor="crosshair")
        self.selection_rect = tk.Frame(
            self.selection_overlay,
            bg="red",
            highlightthickness=1,
            highlightbackground="white",
        )
        self.selection_overlay.bind("<Button-1>", self.on_drag_start)
        self.selection_overlay.bind("<B1-Motion>", self.on_drag_motion)
        self.selection_overlay.bind("<ButtonRelease-1>", self.on_drag_end)

    def on_drag_start(self, event):
        self.drag_start = (event.x_root, event.y_root)
        self.selection_rect.place(x=event.x, y=event.y, width=1, height=1)

    def on_drag_motion(self, event):
        x1, y1 = self.drag_start
        x2, y2 = event.x_root, event.y_root
        x, y = (
            self.selection_overlay.winfo_rootx(),
            self.selection_overlay.winfo_rooty(),
        )
        self.selection_rect.place(
            x=min(x1, x2) - x,
            y=min(y1, y2) - y,
            width=abs(x1 - x2),
            height=abs(y1 - y2),
        )

    def on_drag_end(self, event):
        x1, y1 = self.drag_start
        x2, y2 = event.x_root, event.y_root
        self.game_area = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.selection_overlay.destroy()
        self.root.deiconify()
        self.update_status("Game area set. Press Start to begin.")
        self.preview_btn.config(state=tk.NORMAL)
        self.debug_btn.config(state=tk.NORMAL)
        self.update_area_info()
        self.start_threads()

        self.root.after_idle(
            lambda: self.settings_manager.auto_save_setting("coordinates")
        )

    def start_sell_button_selection(self):
        self.root.iconify()
        self.sell_selection_overlay = tk.Toplevel()
        self.sell_selection_overlay.attributes(
            "-fullscreen", True, "-alpha", 0.3, "-topmost", True
        )
        self.sell_selection_overlay.configure(bg="red", cursor="crosshair")

        instruction_label = Label(
            self.sell_selection_overlay,
            text="FIRST: Press 'G' to open your inventory\nTHEN: Click on the SELL BUTTON",
            font=("Arial", 20, "bold"),
            bg="red",
            fg="white",
        )
        instruction_label.pack(pady=100)

        self.sell_selection_overlay.bind("<Button-1>", self.on_sell_button_click)

    def on_sell_button_click(self, event):
        self.automation_manager.sell_button_position = (event.x_root, event.y_root)
        self.sell_selection_overlay.destroy()
        self.root.deiconify()
        self.update_status(f"Sell button set at ({event.x_root}, {event.y_root})")
        self.update_sell_info()
        self.root.after_idle(
            lambda: self.settings_manager.auto_save_setting("coordinates")
        )

    def start_cursor_position_selection(self):
        self.root.iconify()
        self.cursor_selection_overlay = tk.Toplevel()
        self.cursor_selection_overlay.attributes(
            "-fullscreen", True, "-alpha", 0.3, "-topmost", True
        )
        self.cursor_selection_overlay.configure(bg="blue", cursor="crosshair")

        instruction_label = Label(
            self.cursor_selection_overlay,
            text="Click to set cursor position for clicking",
            font=("Arial", 20, "bold"),
            bg="blue",
            fg="white",
        )
        instruction_label.pack(pady=100)

        self.cursor_selection_overlay.bind("<Button-1>", self.on_cursor_position_click)

    def on_cursor_position_click(self, event):
        self.cursor_position = (event.x_root, event.y_root)
        self.cursor_selection_overlay.destroy()
        self.root.deiconify()
        self.update_status(f"Cursor position set at ({event.x_root}, {event.y_root})")
        self.update_cursor_info()

        self.root.after_idle(
            lambda: self.settings_manager.auto_save_setting("coordinates")
        )

    def test_sell_button_click(self):
        self.automation_manager.test_sell_button_click()

    def show_settings_info(self):
        success = self.settings_manager.open_settings_directory()
        if not success:
            import tkinter.messagebox as messagebox

            settings_info = self.settings_manager.get_settings_info()
            messagebox.showerror(
                "Error",
                f"Could not open settings directory:\n{settings_info['settings_directory']}",
            )

    def test_discord_ping(self):
        try:
            webhook_url = self.param_vars.get('webhook_url', tk.StringVar()).get()
            include_screenshot = self.param_vars.get('include_screenshot_in_discord', tk.BooleanVar()).get()
            user_id = self.param_vars.get('user_id', tk.StringVar()).get()

            if not webhook_url:
                self.update_status("Webhook URL not set!")
                return

            self.update_status("Testing Discord ping...")
            self.discord_notifier.set_webhook_url(webhook_url)

            success = threading.Thread(target=self.discord_notifier.test_webhook, args=(user_id if user_id else None, include_screenshot), daemon=True)
            success.start()

            if success:
                self.update_status("Discord ping test completed successfully!")
            else:
                self.update_status("Discord ping test failed!")

        except Exception as e:
            self.update_status(f"Discord ping test error: {e}")

    def check_milestone_notifications(self):
        try:
            webhook_url = self.param_vars.get('webhook_url', tk.StringVar()).get()
            include_screenshot = self.param_vars.get('include_screenshot_in_discord', tk.BooleanVar()).get()
            user_id = self.param_vars.get('user_id', tk.StringVar()).get()
            milestone_interval = self.param_vars.get('milestone_interval', tk.IntVar()).get()

            if not webhook_url or milestone_interval <= 0:
                return

            if (
                self.dig_count > 0
                and self.dig_count % milestone_interval == 0
                and self.dig_count != self.last_milestone_notification
            ):
                self.discord_notifier.set_webhook_url(webhook_url)
                threading.Thread(target=self.discord_notifier.send_milestone_notification, args=(
                    self.dig_count,
                    self.click_count,
                    user_id if user_id else None,
                    include_screenshot
                ), daemon=True).start()
                self.last_milestone_notification = self.dig_count

        except Exception as e:
            logger.error(f"Error sending milestone notification: {e}")

    def toggle_main_on_top(self, *args):
        self.root.attributes("-topmost", self.param_vars["main_on_top"].get())

    def toggle_preview_on_top(self, *args):
        if self.preview_window:
            self.preview_window.attributes(
                "-topmost", self.param_vars["preview_on_top"].get()
            )

    def toggle_debug_on_top(self, *args):
        if self.debug_window:
            self.debug_window.attributes(
                "-topmost", self.param_vars["debug_on_top"].get()
            )

    def toggle_preview_window(self):
        if self.preview_window is None:
            self.preview_window = tk.Toplevel(self.root)
            self.preview_window.title("Preview")
            self.preview_window.geometry("800x240")
            self.preview_window.configure(bg="black")

            try:
                if os.path.exists("assets/icon.ico"):
                    self.preview_window.wm_iconbitmap("assets/icon.ico")
            except:
                pass

            self.preview_label = Label(self.preview_window, bg="black")
            self.preview_label.pack(fill=tk.BOTH, expand=True)

            velocity_frame = Frame(self.preview_window, bg="black", pady=5)
            self.velocity_info_label = Label(
                velocity_frame,
                text="Velocity: -- px/s | Acceleration: -- px/s²",
                font=("Segoe UI", 9),
                bg="black",
                fg="white",
                anchor="center",
            )
            self.velocity_info_label.pack(expand=True)
            velocity_frame.pack(fill="x")

            self.preview_window.protocol("WM_DELETE_WINDOW", self.toggle_preview_window)
            self.toggle_preview_on_top()
        else:
            self.preview_window.destroy()
            self.preview_window = None
            self.preview_label = None
            self.velocity_info_label = None

    def toggle_debug_window(self):
        if self.debug_window is None:
            self.debug_window = tk.Toplevel(self.root)
            self.debug_window.title("Debug Mask & Detection Info")
            self.debug_window.geometry("800x300")
            self.debug_window.configure(bg="black")

            try:
                if os.path.exists("assets/icon.ico"):
                    self.debug_window.wm_iconbitmap("assets/icon.ico")
            except:
                pass

            self.debug_label = Label(self.debug_window, bg="black")
            self.debug_label.pack(fill=tk.BOTH, expand=True)

            color_frame = Frame(self.debug_window, bg="black", pady=5)
            Label(
                color_frame,
                text="Locked Color:",
                font=("Segoe UI", 9),
                bg="black",
                fg="white",
            ).pack(side="left", padx=5)
            self.color_swatch_label = Label(
                color_frame, text="", bg="black", relief="solid", bd=1, width=15
            )
            self.color_swatch_label.pack(side="left", ipady=5, padx=5)
            color_frame.pack(fill="x")

            detection_frame = Frame(self.debug_window, bg="black", pady=5)
            Label(
                detection_frame,
                text="Detection Method:",
                font=("Segoe UI", 9, "bold"),
                bg="black",
                fg="white",
            ).pack(anchor="w", padx=5)
            self.detection_info_label = Label(
                detection_frame,
                text="Method: Unknown",
                font=("Segoe UI", 8),
                bg="black",
                fg="lightgray",
                justify="left",
            )
            self.detection_info_label.pack(anchor="w", padx=10)
            detection_frame.pack(fill="x")

            self.debug_window.protocol("WM_DELETE_WINDOW", self.toggle_debug_window)
            self.toggle_debug_on_top()
        else:
            self.debug_window.destroy()
            self.debug_window = None
            self.debug_label = None
            self.color_swatch_label = None
            self.detection_info_label = None

    def show_debug_console(self):
        logger.show_console()

    def update_gui_from_queue(self):
        try:
            preview_array, debug_mask, overlay_info = self.results_queue.get_nowait()
            if self.preview_window and self.preview_label:
                pw, ph = (
                    self.preview_label.winfo_width(),
                    self.preview_label.winfo_height(),
                )
                if pw > 20 and ph > 20:
                    img = Image.fromarray(
                        cv2.cvtColor(preview_array, cv2.COLOR_BGR2RGB)
                    )
                    img.thumbnail((pw, ph), Image.Resampling.NEAREST)
                    photo = ImageTk.PhotoImage(image=img)
                    self.preview_label.configure(image=photo)
                    self.preview_label.image = photo

                if hasattr(self, "velocity_info_label") and self.velocity_info_label:
                    velocity = overlay_info.get("velocity", 0)
                    acceleration = overlay_info.get("acceleration", 0)
                    line_detected = overlay_info.get("line_detected", False)

                    velocity_text = f"Velocity: {velocity:.1f} px/s | Acceleration: {acceleration:.1f} px/s²"
                    if not line_detected:
                        velocity_text += " | Line: NOT DETECTED"
                    else:
                        velocity_text += " | Line: DETECTED"

                    self.velocity_info_label.config(text=velocity_text)
            if self.debug_window and self.debug_label and debug_mask is not None:
                dw, dh = self.debug_label.winfo_width(), self.debug_label.winfo_height()
                if dw > 20 and dh > 20:
                    debug_bgr = cv2.cvtColor(debug_mask, cv2.COLOR_GRAY2BGR)
                    debug_img = Image.fromarray(
                        cv2.cvtColor(debug_bgr, cv2.COLOR_BGR2RGB)
                    )
                    debug_img.thumbnail((dw, dh), Image.Resampling.NEAREST)
                    debug_photo = ImageTk.PhotoImage(image=debug_img)
                    self.debug_label.configure(image=debug_photo)
                    self.debug_label.image = debug_photo
            locked_color = overlay_info.get("locked_color_hex")
            if self.color_swatch_label:
                self.color_swatch_label.config(
                    bg=locked_color if locked_color else "#000000"
                )

            if self.detection_info_label and overlay_info.get("detection_info"):
                detection_info = overlay_info["detection_info"]
                method = detection_info.get("method", "Unknown")
                threshold = detection_info.get("threshold", "N/A")

                info_text = f"Method: {method}\nThreshold: {threshold}"

                if "Otsu" in method:
                    if "min_area" in detection_info:
                        info_text += f"\nMin Area: {detection_info['min_area']} px"
                    if "max_area" in detection_info:
                        info_text += f"\nMax Area: {detection_info['max_area']}"
                    if "area_percentile" in detection_info:
                        info_text += f"\nArea %: {detection_info['area_percentile']}"
                    if "morph_kernel" in detection_info:
                        info_text += f"\nMorph Kernel: {detection_info['morph_kernel']}"
                elif "Color Picker" in method:
                    if "target_color" in detection_info:
                        info_text += f"\nTarget Color: {detection_info['target_color']}"
                    if "tolerance" in detection_info:
                        info_text += f"\nTolerance: {detection_info['tolerance']}"
                    if "target_hsv" in detection_info:
                        info_text += f"\nTarget HSV: {detection_info['target_hsv']}"

                self.detection_info_label.config(text=info_text)

            if self.overlay_enabled and self.overlay:
                self.overlay.update_info(**overlay_info)
            if self.autowalk_overlay_enabled and self.autowalk_overlay:
                self.autowalk_overlay.update_info(**overlay_info)
        except (queue.Empty, RuntimeError, TclError):
            pass
        finally:
            if self.preview_active:
                self.root.after(50, self.update_gui_from_queue)

    def toggle_detection(self):
        self.root.after(0, self._toggle_detection_thread_safe)

    def _toggle_detection_thread_safe(self):
        if not self.running:
            if not self.game_area:
                self.update_status("Select game area first")
                return

            self.running = True
            self.update_status("Bot Started...")
            self.click_count = 0
            self.dig_count = 0
            self.velocity_calculator.reset()
            self.automation_manager.walk_pattern_index = 0
            self.automation_manager.sell_count = 0
            
            self.startup_time = time.time() * 1000 
            self._startup_grace_ended = False

            self.automation_manager.shiftlock_state = {
                "shift": False,
                "right_shift": False,
            }

            if self.get_param("debug_clicks_enabled"):
                self.init_debug_log()
            if self.click_lock.locked():
                self.click_lock.release()

            self.target_engaged = False
            self.line_moving_history = []

            try:
                webhook_url = self.param_vars.get("webhook_url", tk.StringVar()).get()
                user_id = self.param_vars.get("user_id", tk.StringVar()).get()
                if webhook_url:
                    self.discord_notifier.set_webhook_url(webhook_url)
                    threading.Thread(target=self.discord_notifier.send_startup_notification, args=(user_id), daemon=True).start()
            except:
                pass

        else:
            self.running = False

            self.update_status("Stopped")

            try:
                webhook_url = self.param_vars.get("webhook_url", tk.StringVar()).get()
                user_id = self.param_vars.get("user_id", tk.StringVar()).get()
                if webhook_url:
                    self.discord_notifier.set_webhook_url(webhook_url)
                    threading.Thread(target=self.discord_notifier.send_shutdown_notification, args=(user_id), daemon=True).start()
            except:
                pass

        self.update_main_button_text()

    def init_debug_log(self):
        try:
            self.ensure_debug_dir()
            with open(self.debug_log_path, "w") as f:
                f.write("Dig Tool Debug Log\n")
                f.write("==================\n")
                f.write(f"Session started at timestamp: {int(time.time())}\n")
                f.write(
                    "Format: Click# | Timestamp | Line_Pos | Velocity | Acceleration | Sweet_Spot_Range | Click_Type | Confidence | Screenshot_File\n"
                )
                f.write("-" * 120 + "\n")
        except Exception as e:
            logger.error(f"Error creating debug log: {e}")

    def update_status(self, text):
        if self.root.winfo_exists():
            self.status_label.config(text=f"Status: {text}")

    def _cleanup_click_threads(self):
        self._click_thread_pool = [t for t in self._click_thread_pool if t.is_alive()]

    def perform_click(self, delay=0):
        perform_click_action(
            delay,
            self.running,
            self.get_param("use_custom_cursor"),
            self.cursor_position,
            self.click_lock,
        )
        self.click_count += 1

    def perform_instant_click(self):
        self._cleanup_click_threads()
        if len(self._click_thread_pool) < self._max_click_threads:
            click_thread = threading.Thread(target=self._instant_click, daemon=True)
            self._click_thread_pool.append(click_thread)
            click_thread.start()

    def _instant_click(self):
        if not self.running:
            return
        if self.get_param("use_custom_cursor") and self.cursor_position:
            try:
                ctypes.windll.user32.SetCursorPos(*self.cursor_position)
            except:
                pass
        send_click()
        self.click_count += 1

    def save_debug_screenshot(
        self,
        screenshot,
        line_pos,
        sweet_spot_start,
        sweet_spot_end,
        zone_y2_cached,
        velocity,
        acceleration,
        prediction_used=False,
        confidence=0.0,
    ):
        if not self.get_param("debug_clicks_enabled"):
            return
        self.ensure_debug_dir()
        filename = save_debug_screenshot(
            screenshot,
            line_pos,
            sweet_spot_start,
            sweet_spot_end,
            zone_y2_cached,
            velocity,
            acceleration,
            prediction_used,
            confidence,
            self.click_count,
            self.debug_dir,
            self.smoothed_zone_x,
            self.smoothed_zone_w,
        )
        if filename:
            log_click_debug(
                self.click_count + 1,
                line_pos,
                velocity,
                acceleration,
                sweet_spot_start,
                sweet_spot_end,
                prediction_used,
                confidence,
                filename,
                self.debug_log_path,
            )

    def _update_time_cache(self):
        now = time.time()
        if now - self._last_time_update > 0.001:
            self._current_time_cache = now
            self._current_time_ms_cache = now * 1000
            self._last_time_update = now

    def run_main_loop(self):
        screenshot_fps = self.get_param("screenshot_fps") or 240
        process_every_nth_frame = 1

        screenshot_delay = 1.0 / screenshot_fps
        final_mask = None

        auto_walk_state = "move"
        move_completed_time = 0
        wait_for_target_start = 0
        current_step_click_enabled = True
        dig_completed_time = 0
        target_disengaged_time = 0
        pending_auto_sell = False
        walk_thread = None
        max_wait_time = 5000
        post_dig_delay = 2000
        click_retry_count = 0
        max_click_retries = 2

        cached_height_80 = None
        cached_zone_y2 = None
        cached_line_area = None
        cached_hsv_area = None
        frame_skip_counter = 0
        click_delay = 0  # UnboundLocalError

        while self.preview_active:
            frame_start_time = time.perf_counter()
            self._update_time_cache()
            current_time_ms = self._current_time_ms_cache
            
            startup_grace_period = 100 
            if (hasattr(self, 'startup_time') and 
                hasattr(self, '_startup_grace_ended') and 
                not self._startup_grace_ended and 
                (current_time_ms - self.startup_time) > startup_grace_period):
                self._startup_grace_ended = True
                if self.running:
                    self.update_status("Bot Running...")

            self._memory_cleanup_counter += 1
            if self._memory_cleanup_counter % 300 == 0:
                import gc

                gc.collect()

            game_fps = max(self.get_param("target_fps"), 1)
            self.velocity_calculator.update_fps(game_fps)

            if self.game_area is None:
                time.sleep(0.01)
                continue

            if self.running and self.automation_manager.should_re_equip_shovel():
                self.automation_manager.re_equip_shovel()

            frame_skip_counter += 1
            should_process_zones = frame_skip_counter % process_every_nth_frame == 0

            capture_start = time.perf_counter()
            screenshot = self.cam.capture(bbox=self.game_area, region_key=self.region_key)
            capture_time = time.perf_counter() - capture_start

            if screenshot is None:
                time.sleep(screenshot_delay)
                continue

            height, width = screenshot.shape[:2]

            if cached_height_80 is None or cached_height_80 != int(height * 0.80):
                cached_height_80 = int(height * 0.80)
                cached_zone_y2 = cached_height_80

            height_80 = cached_height_80
            zone_y2 = cached_zone_y2

            if cached_line_area is None or cached_line_area.shape != (height, width):
                cached_line_area = np.empty((height, width), dtype=np.uint8)

            cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY, dst=cached_line_area)
            line_sensitivity = self.get_param("line_sensitivity")
            line_min_height = 1.0
            line_offset = self.get_param("line_detection_offset") or 5.0
            if isinstance(line_offset, str):
                line_offset = float(line_offset)

            line_pos = find_line_position(
                cached_line_area, line_sensitivity, line_min_height, line_offset
            )

            velocity_line_pos = line_pos
            if line_pos == -1:
                bottom_height = int(height * 0.3)
                bottom_start = height - bottom_height
                bottom_area = cached_line_area[bottom_start:, :]
                velocity_line_pos = find_line_position(
                    bottom_area, line_sensitivity, line_min_height, line_offset
                )
                if velocity_line_pos != -1:
                    velocity_line_pos += 0

            if not hasattr(self, "_line_detection_stats"):
                self._line_detection_stats = {
                    "detected": 0,
                    "failed": 0,
                    "last_positions": [],
                }

            if line_pos == -1:
                self._line_detection_stats["failed"] += 1
            else:
                self._line_detection_stats["detected"] += 1
                self._line_detection_stats["last_positions"].append(line_pos)
                if len(self._line_detection_stats["last_positions"]) > 10:
                    self._line_detection_stats["last_positions"].pop(0)

            if should_process_zones:
                if cached_hsv_area is None or cached_hsv_area.shape != (
                    height_80,
                    width,
                    3,
                ):
                    cached_hsv_area = np.empty((height_80, width, 3), dtype=np.uint8)

                zone_detection_area = screenshot[:height_80, :]
                cv2.cvtColor(
                    zone_detection_area, cv2.COLOR_BGR2HSV, dst=cached_hsv_area
                )
                hsv = cached_hsv_area

                saturation_threshold = self.get_param("saturation_threshold")

                if not self.is_color_locked:
                    if final_mask is None or final_mask.shape != (height_80, width):
                        final_mask = np.empty((height_80, width), dtype=np.uint8)

                    use_otsu = self.get_param("use_otsu_detection")
                    use_color_picker = self.get_param("use_color_picker_detection")
                    detection_info = {}

                    if use_color_picker:
                        picked_color = self.get_param("picked_color_rgb")
                        if picked_color and picked_color.strip() and picked_color != "":
                            try:
                                picked_color = picked_color.strip()
                                
                                if picked_color.startswith("#"):
                                    picked_color = picked_color[1:]
                                    
                                if len(picked_color) != 6:
                                    raise ValueError(f"Invalid hex color length: {len(picked_color)}")
                                    
                                rgb_color = int(picked_color, 16)
                                target_hsv = rgb_to_hsv_single(rgb_color)
                                
                                color_tolerance_param = self.get_param("color_tolerance")
                                try:
                                    if isinstance(color_tolerance_param, str):
                                        color_tolerance = int(float(color_tolerance_param.strip())) if color_tolerance_param.strip() else 30
                                    else:
                                        color_tolerance = int(color_tolerance_param) if color_tolerance_param is not None else 30
                                except (ValueError, TypeError):
                                    color_tolerance = 30

                                final_mask = detect_by_color_picker(
                                    hsv, target_hsv, color_tolerance, False
                                )
                                
                                detected_pixels = np.sum(final_mask > 0) if final_mask is not None else 0
                                
                                detection_info = {
                                    "method": "Color Picker",
                                    "target_color": f"#{picked_color}",
                                    "tolerance": color_tolerance,
                                    "target_hsv": f"H:{target_hsv[0]} S:{target_hsv[1]} V:{target_hsv[2]}",
                                    "detected_pixels": detected_pixels,
                                }
                            except (ValueError, TypeError) as e:
                                logger.warning(f"Color picker detection failed: {e}")
                                saturation = hsv[:, :, 1]
                                cv2.threshold(
                                    saturation,
                                    saturation_threshold,
                                    255,
                                    cv2.THRESH_BINARY,
                                    dst=final_mask,
                                )
                                detection_info = {
                                    "method": "Saturation (Fallback)",
                                    "threshold": saturation_threshold,
                                    "error": f"Color picker failed: {e}",
                                }
                        else:
                            saturation = hsv[:, :, 1]
                            cv2.threshold(
                                saturation,
                                saturation_threshold,
                                255,
                                cv2.THRESH_BINARY,
                                dst=final_mask,
                            )
                            detection_info = {
                                "method": "Saturation (No Color Picked)",
                                "threshold": saturation_threshold,
                            }
                    elif use_otsu:
                        if self.get_param("otsu_adaptive_area"):
                            area_percentile = float(
                                self.get_param("otsu_area_percentile")
                            )
                            morph_kernel = int(self.get_param("otsu_morph_kernel_size"))
                            final_mask, threshold_value = detect_by_otsu_adaptive_area(
                                hsv,
                                area_percentile=area_percentile,
                                morph_kernel_size=morph_kernel,
                            )
                            detection_info = {
                                "method": "Otsu (Adaptive)",
                                "threshold": threshold_value,
                                "area_percentile": area_percentile,
                                "morph_kernel": morph_kernel,
                            }
                        else:
                            min_area = int(self.get_param("otsu_min_area"))
                            max_area_param = self.get_param("otsu_max_area")
                            if (
                                max_area_param == ""
                                or max_area_param == "None"
                                or max_area_param == 0
                                or max_area_param is None
                            ):
                                max_area = None
                            else:
                                try:
                                    max_area = int(max_area_param)
                                except (ValueError, TypeError):
                                    max_area = None
                            morph_kernel = int(self.get_param("otsu_morph_kernel_size"))
                            final_mask, threshold_value = (
                                detect_by_otsu_with_area_filter(
                                    hsv,
                                    min_area=min_area,
                                    max_area=max_area,
                                    morph_kernel_size=morph_kernel,
                                )
                            )
                            detection_info = {
                                "method": "Otsu (Fixed Area)",
                                "threshold": threshold_value,
                                "min_area": min_area,
                                "max_area": (
                                    max_area if max_area is not None else "Unlimited"
                                ),
                                "morph_kernel": morph_kernel,
                            }
                    else:
                        saturation = hsv[:, :, 1]
                        cv2.threshold(
                            saturation,
                            saturation_threshold,
                            255,
                            cv2.THRESH_BINARY,
                            dst=final_mask,
                        )
                        detection_info = {
                            "method": "Saturation Threshold",
                            "threshold": saturation_threshold,
                        }

                    line_exclusion_radius = self.get_param("line_exclusion_radius") or 0
                    if line_exclusion_radius > 0 and line_pos != -1:
                        cv2.rectangle(
                            final_mask,
                            (max(0, line_pos - line_exclusion_radius), 0),
                            (min(width, line_pos + line_exclusion_radius), height_80),
                            0,
                            -1,
                        )

                    if not use_otsu and line_exclusion_radius > 0:
                        kernel_size = max(3, int(min(width, height) * 0.008))
                        if (
                            self._cached_kernel is None
                            or self._cached_kernel_size != kernel_size
                        ):
                            self._cached_kernel = cv2.getStructuringElement(
                                cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
                            )
                            self._cached_kernel_size = kernel_size
                        cv2.morphologyEx(
                            final_mask,
                            cv2.MORPH_CLOSE,
                            self._cached_kernel,
                            dst=final_mask,
                            iterations=2,
                        )
                        cv2.morphologyEx(
                            final_mask,
                            cv2.MORPH_OPEN,
                            self._cached_kernel,
                            dst=final_mask,
                            iterations=1,
                        )
                else:
                    if (
                        self._last_hsv_color is None
                        or not np.array_equal(
                            self.locked_color_hsv, self._last_hsv_color
                        )
                        or self._last_is_low_sat != self.is_low_sat_lock
                    ):
                        self._hsv_lower_bound_cache, self._hsv_upper_bound_cache = (
                            get_hsv_bounds(self.locked_color_hsv, self.is_low_sat_lock)
                        )
                        self._last_hsv_color = self.locked_color_hsv.copy()
                        self._last_is_low_sat = self.is_low_sat_lock
                    lower_bound, upper_bound = (
                        self._hsv_lower_bound_cache,
                        self._hsv_upper_bound_cache,
                    )
                    if final_mask is None or final_mask.shape != (height_80, width):
                        final_mask = np.empty((height_80, width), dtype=np.uint8)
                    cv2.inRange(hsv, lower_bound, upper_bound, dst=final_mask)

                    line_exclusion_radius = self.get_param("line_exclusion_radius") or 0
                    if line_exclusion_radius > 0 and line_pos != -1:
                        cv2.rectangle(
                            final_mask,
                            (max(0, line_pos - line_exclusion_radius), 0),
                            (min(width, line_pos + line_exclusion_radius), height_80),
                            0,
                            -1,
                        )

                cv2.morphologyEx(
                    final_mask,
                    cv2.MORPH_CLOSE,
                    self._kernel,
                    dst=final_mask,
                    iterations=2,
                )
                contours, _ = cv2.findContours(
                    final_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                raw_zone_x, raw_zone_w = None, None
                if contours:
                    main_contour = max(contours, key=cv2.contourArea)
                    x_temp, y_temp, w_temp, h_temp = cv2.boundingRect(main_contour)

                    zone_min_width = self.get_param("zone_min_width")
                    max_zone_width = width * (
                        self.get_param("max_zone_width_percent") / 100.0
                    )
                    min_zone_height = height_80 * (
                        self.get_param("min_zone_height_percent") / 100.0
                    )

                    if (
                        w_temp > zone_min_width
                        and w_temp < max_zone_width
                        and h_temp >= min_zone_height
                    ):
                        raw_zone_x, raw_zone_w = x_temp, w_temp
                        if not self.is_color_locked:
                            mask = np.zeros(hsv.shape[:2], dtype="uint8")
                            cv2.drawContours(mask, [main_contour], -1, 255, -1)
                            mean_hsv = cv2.mean(hsv, mask=mask)
                            self.locked_color_hsv = np.array(
                                mean_hsv[:3], dtype=np.float32
                            )
                            self.is_color_locked = True
                            bgr_color = cv2.cvtColor(
                                np.uint8([[self.locked_color_hsv]]), cv2.COLOR_HSV2BGR
                            )[0][0]
                            self.locked_color_hex = f"#{bgr_color[2]:02x}{bgr_color[1]:02x}{bgr_color[0]:02x}"
                            self.is_low_sat_lock = self.locked_color_hsv[1] < 25
            else:
                raw_zone_x, raw_zone_w = None, None

            if raw_zone_x is not None:
                self.automation_manager.update_target_lock_activity()
                self.frames_since_last_zone_detection = 0

                zone_smoothing_factor = self.get_param("zone_smoothing_factor")

                if self.smoothed_zone_x is None:
                    self.smoothed_zone_x, self.smoothed_zone_w = raw_zone_x, raw_zone_w
                else:
                    position_change = abs(raw_zone_x - self.smoothed_zone_x)
                    width_change = abs(raw_zone_w - self.smoothed_zone_w)

                    if zone_smoothing_factor >= 1.0:
                        adaptive_smoothing = 1.0 
                    elif zone_smoothing_factor <= 0.01:
                        adaptive_smoothing = zone_smoothing_factor 
                    else:
                        max_change_threshold = width * 0.1
                        if (
                            position_change > max_change_threshold
                            or width_change > max_change_threshold
                        ):
                            adaptive_smoothing = min(zone_smoothing_factor + 0.1, 1.0)
                        else:
                            adaptive_smoothing = zone_smoothing_factor

                    self.smoothed_zone_x = (
                        adaptive_smoothing * raw_zone_x
                        + (1 - adaptive_smoothing) * self.smoothed_zone_x
                    )
                    self.smoothed_zone_w = (
                        adaptive_smoothing * raw_zone_w
                        + (1 - adaptive_smoothing) * self.smoothed_zone_w
                    )
            else:
                self.frames_since_last_zone_detection += 1

            zone_timeout_frames = max(int(game_fps * 0.167), 5)
            if self.frames_since_last_zone_detection > zone_timeout_frames:
                self.is_color_locked = False
                self.locked_color_hsv = None
                self.locked_color_hex = None
                self.smoothed_zone_x = None

            velocity = self.velocity_calculator.add_position(
                velocity_line_pos, self._current_time_cache
            )
            acceleration = self.velocity_calculator.get_acceleration()

            display_velocity = 0.0
            if len(self.velocity_calculator.position_history) >= 2:
                recent_positions = list(self.velocity_calculator.position_history)[-2:]
                if len(recent_positions) == 2:
                    pos1, t1 = recent_positions[0]
                    pos2, t2 = recent_positions[1]
                    if pos1 != -1 and pos2 != -1 and t2 > t1:
                        display_velocity = (pos2 - pos1) / (t2 - t1)

            self.target_engaged = self.check_target_engagement(line_pos, game_fps)

            sweet_spot_center, sweet_spot_start, sweet_spot_end = None, None, None
            if self.smoothed_zone_x is not None:
                sweet_spot_center = self.smoothed_zone_x + self.smoothed_zone_w / 2

                base_sweet_spot_width_percent = (
                    self.get_param("sweet_spot_width_percent") / 100.0
                )
                dynamic_sweet_spot_width_percent = (
                    calculate_velocity_based_sweet_spot_width(
                        base_sweet_spot_width_percent * 100.0,
                        velocity,
                        enabled=self.get_param("velocity_based_width_enabled"),
                        velocity_multiplier=self.get_param("velocity_width_multiplier"),
                        max_velocity_factor=self.get_param("velocity_max_factor"),
                    )
                    / 100.0
                )
                sweet_spot_width = (
                    self.smoothed_zone_w * dynamic_sweet_spot_width_percent
                )
                sweet_spot_start = sweet_spot_center - sweet_spot_width / 2
                sweet_spot_end = sweet_spot_center + sweet_spot_width / 2

            if pending_auto_sell and dig_completed_time > 0:
                time_since_pending = current_time_ms - dig_completed_time
                if time_since_pending > 10000:
                    logger.warning(
                        f"Auto-sell pending timeout ({time_since_pending}ms) - clearing stuck state"
                    )
                    pending_auto_sell = False
                    dig_completed_time = 0

            if (
                self.running
                and self.get_param("auto_walk_enabled")
                and not self.automation_manager.is_selling
            ):
                if auto_walk_state == "move":
                    if (
                        pending_auto_sell
                        and dig_completed_time > 0
                        and current_time_ms - dig_completed_time >= post_dig_delay
                    ):
                        logger.debug(
                            f"Initiating auto-sell: dig_count={self.dig_count}, sell_every_x_digs={self.get_param('sell_every_x_digs')}"
                        )

                        if self.automation_manager.is_auto_sell_ready():
                            threading.Thread(
                                target=self.automation_manager.perform_auto_sell,
                                daemon=True,
                            ).start()
                            pending_auto_sell = False
                            dig_completed_time = 0

                            wait_start_time = time.time()
                            auto_sell_max_wait_time = 30.0

                            while (
                                self.automation_manager.is_selling
                                and self.running
                                and (time.time() - wait_start_time)
                                < auto_sell_max_wait_time
                            ):
                                time.sleep(0.1)

                            if (
                                time.time() - wait_start_time
                            ) >= auto_sell_max_wait_time:
                                logger.warning(
                                    "Auto-sell wait timeout reached, resuming autowalk"
                                )
                        else:
                            logger.warning(
                                "Auto-sell skipped: not ready (sell button, running state, or already selling)"
                            )
                            pending_auto_sell = False
                            dig_completed_time = 0

                    elif (
                        not self.automation_manager.is_selling and not pending_auto_sell
                    ):
                        if walk_thread is None or not walk_thread.is_alive():
                            direction = (
                                self.automation_manager.get_next_walk_direction()
                            )

                            if isinstance(direction, dict):
                                current_step_click_enabled = direction.get(
                                    "click", True
                                )
                            else:
                                current_step_click_enabled = True

                            def perform_walk_with_callback():
                                if (
                                    self.automation_manager.is_selling
                                    or not self.running
                                ):
                                    logger.debug(
                                        "Walk step aborted - selling in progress or tool stopped"
                                    )
                                    return

                                if isinstance(direction, dict):
                                    key = direction.get("key", "")
                                    custom_duration = direction.get("duration", None)
                                    if custom_duration is not None:
                                        success = self.automation_manager._execute_movement_with_duration(
                                            key, custom_duration / 1000.0
                                        )
                                    else:
                                        success = (
                                            self.automation_manager.perform_walk_step(
                                                key
                                            )
                                        )
                                else:
                                    success = self.automation_manager.perform_walk_step(
                                        direction
                                    )

                                if success:
                                    self.automation_manager.advance_walk_pattern()

                            walk_thread = threading.Thread(
                                target=perform_walk_with_callback, daemon=True
                            )
                            walk_thread.start()

                            auto_walk_state = "click_to_start"

                            if (
                                isinstance(direction, dict)
                                and direction.get("duration") is not None
                            ):
                                walk_duration = direction.get("duration")
                            else:
                                walk_duration = self.get_param("walk_duration")

                            move_completed_time = current_time_ms + walk_duration + 300

                elif (
                    auto_walk_state == "click_to_start"
                    and current_time_ms >= move_completed_time
                    and not self.automation_manager.is_selling
                ):
                    if current_step_click_enabled:
                        if not self.click_lock.locked():
                            self.click_lock.acquire()
                            threading.Thread(
                                target=self.perform_click, args=(click_delay,)
                            ).start()
                            auto_walk_state = "wait_for_target"
                            wait_for_target_start = current_time_ms
                    else:
                        logger.debug("Skipping click for this step (click disabled)")
                        auto_walk_state = "move"

                elif (
                    auto_walk_state == "wait_for_target"
                    and not self.automation_manager.is_selling
                ):
                    if self.target_engaged:
                        auto_walk_state = "digging"
                        target_disengaged_time = 0
                        click_retry_count = 0
                    elif current_time_ms - wait_for_target_start > max_wait_time:
                        if click_retry_count < max_click_retries:
                            click_retry_count += 1
                            logger.debug(
                                f"Target engagement timeout - retry {click_retry_count}/{max_click_retries}"
                            )

                            if not self.click_lock.locked():
                                self.click_lock.acquire()
                                threading.Thread(
                                    target=self.perform_click, args=(0,)
                                ).start()
                                wait_for_target_start = current_time_ms
                        else:
                            logger.warning(
                                f"Target engagement failed after {max_click_retries} retries - advancing pattern"
                            )
                            click_retry_count = 0
                            auto_walk_state = "move"

                elif auto_walk_state == "digging":
                    if not self.target_engaged:
                        if target_disengaged_time == 0:
                            target_disengaged_time = current_time_ms
                    else:
                        target_disengaged_time = 0

            should_allow_clicking = True
            if self.get_param("auto_walk_enabled"):
                should_allow_clicking = (
                    auto_walk_state == "digging"
                    and not self.automation_manager.is_selling
                    and self.target_engaged
                )
            else:
                should_allow_clicking = self.target_engaged

            post_click_blindness = self.get_param("post_click_blindness")
            
            startup_grace_period = 100  
            is_past_startup_grace = not hasattr(self, 'startup_time') or (current_time_ms - self.startup_time) > startup_grace_period

            if (
                self.running
                and should_allow_clicking
                and current_time_ms >= self.blind_until
                and sweet_spot_center is not None
                and not self.click_lock.locked()
                and is_past_startup_grace 
            ):

                should_click, click_delay, prediction_used, confidence = (
                    False,
                    0,
                    False,
                    0.0,
                )

                line_in_sweet_spot = sweet_spot_start <= line_pos <= sweet_spot_end

                if self.get_param("prediction_enabled") and line_pos != -1:
                    prediction_confidence_threshold = self.get_param(
                        "prediction_confidence_threshold"
                    )
                    system_latency = self.get_cached_system_latency() / 1000.0

                    is_moving_towards = (
                        line_pos < sweet_spot_center and velocity > 0
                    ) or (line_pos > sweet_spot_center and velocity < 0)

                    if is_moving_towards:
                        predicted_pos, prediction_time = (
                            self.velocity_calculator.predict_position(
                                line_pos, sweet_spot_center, self._current_time_cache
                            )
                        )

                        if prediction_time > 0:
                            distance_to_center = abs(predicted_pos - sweet_spot_center)
                            sweet_spot_radius = (sweet_spot_end - sweet_spot_start) / 2

                            if distance_to_center <= sweet_spot_radius:
                                base_confidence = max(
                                    0.0, 1.0 - (distance_to_center / sweet_spot_radius)
                                )
                                velocity_confidence = (
                                    self.velocity_calculator.get_prediction_confidence(
                                        line_pos,
                                        sweet_spot_center,
                                        predicted_pos,
                                        prediction_time,
                                        game_fps,
                                    )
                                )

                                confidence = base_confidence * velocity_confidence

                                fps_adjusted_threshold = (
                                    prediction_confidence_threshold
                                    * (game_fps / 120.0) ** 0.15
                                )

                                if confidence >= fps_adjusted_threshold:
                                    fps_latency_adjustment = (
                                        system_latency * (120.0 / game_fps) * 0.8
                                    )
                                    sleep_duration = (
                                        prediction_time - fps_latency_adjustment
                                    )

                                    if sleep_duration > 0:
                                        should_click, click_delay, prediction_used = (
                                            True,
                                            sleep_duration,
                                            True,
                                        )

                if not should_click and line_in_sweet_spot:
                    should_click = True
                    confidence = 1.0

                if should_click:
                    self.automation_manager.update_click_activity()
                    self.save_debug_screenshot(
                        screenshot,
                        line_pos,
                        sweet_spot_start,
                        sweet_spot_end,
                        zone_y2,
                        velocity,
                        acceleration,
                        prediction_used,
                        confidence,
                    )
                    self.blind_until = current_time_ms + post_click_blindness

                    if click_delay == 0:
                        self.perform_instant_click()
                    else:
                        self.click_lock.acquire()
                        threading.Thread(
                            target=self.perform_click, args=(click_delay,)
                        ).start()

            if (
                self.get_param("auto_walk_enabled")
                and auto_walk_state == "digging"
                and target_disengaged_time > 0
                and current_time_ms - target_disengaged_time > 1500
            ):
                self.dig_count += 1
                self.automation_manager.update_dig_activity()
                dig_completed_time = current_time_ms

                auto_sell_enabled = self.get_param("auto_sell_enabled")
                sell_every_x_digs = self.get_param("sell_every_x_digs")
                has_sell_button = (
                    self.automation_manager.sell_button_position is not None
                )

                logger.info(
                    f"Dig completed #{self.dig_count}: auto_sell_enabled={auto_sell_enabled}, sell_button_set={has_sell_button}, sell_every_x_digs={sell_every_x_digs}"
                )

                if (
                    auto_sell_enabled
                    and has_sell_button
                    and self.dig_count > 0
                    and self.dig_count % sell_every_x_digs == 0
                ):
                    pending_auto_sell = True
                    logger.info(
                        f"Auto-sell triggered! Will sell after {post_dig_delay}ms delay"
                    )

                auto_walk_state = "move"
                self.check_milestone_notifications()

            if self.results_queue.empty():
                preview_img = screenshot.copy()
                if sweet_spot_center is not None:
                    cv2.rectangle(
                        preview_img,
                        (int(self.smoothed_zone_x), 0),
                        (int(self.smoothed_zone_x + self.smoothed_zone_w), zone_y2),
                        (0, 255, 0),
                        2,
                    )
                    cv2.rectangle(
                        preview_img,
                        (int(sweet_spot_start), 0),
                        (int(sweet_spot_end), zone_y2),
                        (0, 255, 255),
                        2,
                    )
                if line_pos != -1:
                    cv2.line(
                        preview_img, (line_pos, 0), (line_pos, height), (0, 0, 255), 1
                    )
                h, w = preview_img.shape[:2]
                thumbnail = cv2.resize(
                    preview_img,
                    (150, int(150 * h / w)),
                    interpolation=cv2.INTER_NEAREST,
                )

                overlay_info = {
                    "sweet_spot_center": sweet_spot_center,
                    "velocity": display_velocity,
                    "acceleration": acceleration,  #
                    "click_count": self.click_count,
                    "locked_color_hex": self.locked_color_hex,
                    "preview_thumbnail": thumbnail,
                    "dig_count": self.dig_count,
                    "automation_status": self.automation_manager.get_current_status(),
                    "sell_count": self.automation_manager.sell_count,
                    "target_engaged": self.target_engaged,
                    "line_detected": line_pos != -1,
                    # 'benchmark_fps': self.benchmark_fps,
                    "detection_info": (
                        detection_info
                        if "detection_info" in locals()
                        else {"method": "Unknown", "threshold": "N/A"}
                    ),
                }
                try:
                    self.results_queue.put_nowait(
                        (preview_img, final_mask, overlay_info)
                    )
                except queue.Full:
                    pass

             # Benchmarking
            now = time.time()
            frame_time = frame_start_time - self.last_frame_time
            self.last_frame_time = frame_start_time

            self.frame_times.append(frame_time)

            if now - self.last_report_time >= self.report_interval:
                if self.frame_times:
                    avg_frame_time = sum(self.frame_times) / len(self.frame_times)
                    self.benchmark_fps = int(1.0 / avg_frame_time) if avg_frame_time > 0 else 0
                    # logger.debug(f"Benchmark: {self.benchmark_fps} FPS (avg frame time: {avg_frame_time*1000:.2f}ms)")
                    self.frame_times.clear()
                self.last_report_time = now
            elapsed = time.perf_counter() - frame_start_time

            if screenshot_delay > elapsed:
                time.sleep(screenshot_delay - elapsed)

    def run(self):
        self.root.mainloop()
        
    def perform_initial_latency_measurement(self):
        try:
            logger.info("Performing initial system latency measurement...")
            measured_latency = self.measure_system_latency()
            self._cached_latency = measured_latency
            logger.info(f"System latency measured: {measured_latency}ms")
            return measured_latency
        except Exception as e:
            logger.warning(f"Could not measure system latency automatically: {e}")
            default_latency = 50
            self._cached_latency = default_latency
            return default_latency

    def get_cached_system_latency(self):
        if hasattr(self, "_cached_latency") and hasattr(
            self, "_latency_measurement_time"
        ):
            if time.time() - self._latency_measurement_time < 300:
                return self._cached_latency

        if not hasattr(self, "_cached_latency") or not hasattr(
            self, "_latency_measurement_time"
        ):
            logger.info("Measuring system latency (one-time measurement)...")
            measured_latency = self.measure_system_latency()
            self._cached_latency = measured_latency
            self._latency_measurement_time = time.time()
            return measured_latency

        return self._cached_latency

    def _initialize_default_param_vars(self):
        for key, default_value in self.settings_manager.default_params.items():
            var_type = self.settings_manager.get_param_type(key)
            self.param_vars[key] = var_type(value=default_value)
            self.last_known_good_params[key] = default_value

        for key, default_value in self.settings_manager.default_keybinds.items():
            self.keybind_vars[key] = tk.StringVar(value=default_value)

    def _check_and_enable_buttons(self):
        if (
            self.game_area
            and hasattr(self, "preview_btn")
            and hasattr(self, "debug_btn")
        ):
            self.preview_btn.config(state=tk.NORMAL)
            self.debug_btn.config(state=tk.NORMAL)

            if not self.main_loop_thread or not self.main_loop_thread.is_alive():
                self.start_threads()



if __name__ == "__main__":
    app = DigTool()
    app.run()
