"""A Tk window with one box that can be dragged on a canvas, for the drag desktop tests.

Usage: dragapp.py OUT.json. Writes {"ready": ..., "start": [x, y], "drop": [x, y]} once shown (screen
pixels), then {"released": true, "box": [x0, y0, x1, y1], "moves": n} after every release of the box.
"""

import ctypes
import json
import sys
import tkinter as tk
from pathlib import Path

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels, like Overshoulder
out = Path(sys.argv[1])
root = tk.Tk()
root.title("Overshoulder drag test")
root.geometry("420x300+520+260")
canvas = tk.Canvas(root, width=400, height=280, background="white", highlightthickness=0)
canvas.pack()
box = canvas.create_rectangle(30, 30, 90, 90, fill="#2563eb", outline="")
state = {"at": None, "moves": 0}


def press(e):
    if box in canvas.find_overlapping(e.x, e.y, e.x, e.y):
        state["at"] = (e.x, e.y)


def move(e):
    if state["at"]:
        canvas.move(box, e.x - state["at"][0], e.y - state["at"][1])
        state["at"] = (e.x, e.y)
        state["moves"] += 1


def release(e):
    if state["at"]:
        state["at"] = None
        out.write_text(json.dumps({"released": True, "box": canvas.coords(box), "moves": state["moves"]}))


canvas.bind("<ButtonPress-1>", press)
canvas.bind("<B1-Motion>", move)
canvas.bind("<ButtonRelease-1>", release)


def ready():
    x, y = canvas.winfo_rootx(), canvas.winfo_rooty()
    out.write_text(json.dumps({"ready": True, "start": [x + 60, y + 60], "drop": [x + 300, y + 200]}))


root.after(300, ready)
root.mainloop()
