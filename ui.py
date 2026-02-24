import tkinter as tk
import psutil as ps
root = tk.Tk()
root.title("Real-Time OS Monitor")
root.geometry("600x400")

top_frame = tk.Frame(root)
top_frame.pack(fill="x", pady=10)

middle_frame = tk.Frame(root)
middle_frame.pack(fill="both", expand=True)

cpu_label = tk.Label(top_frame, text="CPU Usage: ", font=("Arial", 14))
cpu_label.pack()

memory_label = tk.Label(top_frame, text="Memory Usage: ", font=("Arial", 14))
memory_label.pack()

process_label = tk.Label(middle_frame, text="Process Table Coming Soon", font=("Arial", 12))
process_label.pack()

def update_data():
    cpu = ps.cpu_percent(interval = None)
    memory = ps.virtual_memory().percent
    cpu_label.config(text = f"CPU Usage: {cpu}%")
    memory_label.config(text=f"Memory Usage: {memory}%")
    root.after(1000, update_data)
update_data()

root.mainloop()