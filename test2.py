import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import cv2
import mss
import numpy as np


class ScreenRecorder:
    def __init__(self, root_window):
        self.root = root_window
        self.root.title("Python Screen Recorder")

        # --- State Variables ---
        self.status_var = tk.StringVar(value="Idle")
        self.is_recording = False
        self.video_writer = None
        self.recording_thread = None
        self.sct = None  # mss instance
        self.filepath = None  # To store the path of the current recording

        # --- Configuration ---
        self.fps = 20.0  # Desired frames per second
        self.screen_width = 0
        self.screen_height = 0
        self.monitor_info = {}

        # --- UI Elements ---
        self.root.geometry("360x180")  # Adjusted height for padding
        # Allow window to be resizable but fix elements via packing
        self.root.minsize(360, 180)

        main_frame = ttk.Frame(self.root, padding="20 20 20 20")
        main_frame.pack(expand=True, fill=tk.BOTH)
        main_frame.columnconfigure(0, weight=1)  # Make label expand if needed

        self.status_label = ttk.Label(
            main_frame,
            textvariable=self.status_var,
            font=("Helvetica", 13),
            justify=tk.CENTER,
            wraplength=300,  # Wrap text if too long
        )
        self.status_label.pack(pady=(0, 20), fill=tk.X)

        button_frame = ttk.Frame(main_frame)
        button_frame.pack(pady=10)

        self.start_button = ttk.Button(
            button_frame,
            text="Start",
            command=self.start_recording_thread_safe,  # Use thread-safe starter
        )
        self.start_button.pack(side=tk.LEFT, padx=10)

        self.stop_button = ttk.Button(
            button_frame,
            text="Stop",
            command=self.stop_recording_thread_safe,  # Use thread-safe stopper
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=10)

        # Handle window close event
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _get_screen_dimensions(self):
        """Gets dimensions of the primary monitor and stores them."""
        with mss.mss() as sct_temp:
            # monitor[0] is all monitors together, monitor[1] is the primary
            if len(sct_temp.monitors) > 1:
                self.monitor_info = sct_temp.monitors[1]
            else:  # Fallback if only one monitor entry (e.g. virtual display)
                self.monitor_info = sct_temp.monitors[0]

            self.screen_width = self.monitor_info["width"]
            self.screen_height = self.monitor_info["height"]

    def _update_ui_for_recording_start(self):
        """Updates UI elements when recording starts. Must be called from main thread."""
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

    def _update_ui_for_recording_stop(self):
        """Updates UI elements when recording stops. Must be called from main thread."""
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

    def start_recording_thread_safe(self):
        """Starts the recording process in a new thread."""
        if self.is_recording:
            return

        self.is_recording = True
        self._update_ui_for_recording_start()
        self.status_var.set("Initializing...")

        self.recording_thread = threading.Thread(target=self._record_worker)
        self.recording_thread.daemon = True  # Allow main program to exit
        self.recording_thread.start()

    def _record_worker(self):
        """The actual recording logic that runs in a separate thread."""
        video_writer_initialized_successfully = False
        try:
            self._get_screen_dimensions()  # Get/update screen dimensions

            movies_dir = Path.home() / "Movies"
            movies_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime(
                "%Y-%m-%dT%H-%M-%S"
            )  # ISO8601 like, filesystem-friendly
            filename = f"Screen-{timestamp}.mp4"
            self.filepath = movies_dir / filename

            # Define the codec (Motion JPEG is widely compatible for .mp4)
            # Other options: 'XVID' (for .avi), 'mp4v' (for .mp4)
            # 'avc1' or 'h264' if FFmpeg is correctly linked with OpenCV
            fourcc = cv2.VideoWriter_fourcc(
                *"MJPG"
            )  # Using MJPEG for wider compatibility in .mp4
            # Alternatively, use 'mp4v'
            self.video_writer = cv2.VideoWriter(
                str(self.filepath),
                fourcc,
                self.fps,
                (self.screen_width, self.screen_height),
            )

            if not self.video_writer.isOpened():
                raise IOError(
                    f"Cannot open video writer for {self.filepath}. Check codec and permissions."
                )
            video_writer_initialized_successfully = True

            self.sct = mss.mss()  # Initialize mss for this thread

            self.root.after(0, lambda: self.status_var.set(f"Recording → {filename}"))
            print(f"📹 Recording to: {self.filepath}")

            while self.is_recording:  # Loop controlled by self.is_recording flag
                start_frame_time = time.perf_counter()

                img_bgra = self.sct.grab(self.monitor_info)  # Capture screen (BGRA)
                frame_np = np.array(img_bgra)  # Convert to NumPy array
                frame_bgr = cv2.cvtColor(
                    frame_np, cv2.COLOR_BGRA2BGR
                )  # Convert to BGR for OpenCV

                self.video_writer.write(frame_bgr)

                # Frame rate control
                elapsed_time = time.perf_counter() - start_frame_time
                sleep_time = (1.0 / self.fps) - elapsed_time
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # else:
                # print(f"Warning: Frame capture/processing took too long: {elapsed_time*1000:.2f}ms")

        except Exception as e:
            error_message = f"Failed: {str(e)}"
            print(f"Error in recording worker: {error_message}")
            self.root.after(0, lambda em=error_message: self.status_var.set(em))
        finally:
            # Cleanup (runs in the worker thread)
            if self.sct:
                self.sct = None  # Dereference, mss handles its own cleanup

            if self.video_writer:
                self.video_writer.release()
                self.video_writer = None

            # Determine final status and update UI (scheduled on main thread)
            final_status_msg = "Stopped."  # Default
            current_ui_status = (
                self.status_var.get()
            )  # Get status which might have been set by exception

            if current_ui_status.startswith("Failed:"):
                final_status_msg = current_ui_status
            elif (
                video_writer_initialized_successfully
                and self.filepath
                and Path(self.filepath).exists()
            ):
                # If stop_recording was called and writer was active and file exists
                final_status_msg = f"Saved: {Path(self.filepath).name}"
            elif (
                not video_writer_initialized_successfully
                and not current_ui_status.startswith("Failed:")
            ):
                final_status_msg = "Stopped before recording fully started."

            self.root.after(0, lambda fsm=final_status_msg: self.status_var.set(fsm))
            self.root.after(0, self._update_ui_for_recording_stop)
            self.is_recording = False  # Ensure flag is reset after thread finishes

    def stop_recording_thread_safe(self):
        """Signals the recording thread to stop."""
        if not self.is_recording:
            return

        self.status_var.set("Stopping...")
        # Disable stop button immediately to prevent multiple clicks, will be re-enabled by worker
        if hasattr(self, "stop_button"):
            self.stop_button.config(state=tk.DISABLED)

        self.is_recording = False  # Signal the worker thread to terminate its loop
        # The worker thread's `finally` block will handle cleanup and final UI updates.

    def on_close(self):
        """Handles the window close event."""
        if self.is_recording:
            print("Attempting to stop recording before closing...")
            self.stop_recording_thread_safe()
            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=3.0)  # Wait for graceful shutdown
                if self.recording_thread.is_alive():
                    print("Recording thread did not stop in time. Forcing close.")
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenRecorder(root)
    root.mainloop()
