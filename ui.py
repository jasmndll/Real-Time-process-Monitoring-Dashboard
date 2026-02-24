import tkinter as tk
import psutil as ps
from tkinter import ttk
# root = tk.Tk()
# root.title("Real-Time OS Monitor")
# root.geometry("600x400")

# top_frame = tk.Frame(root)
# top_frame.pack(fill="x", pady=10)

# middle_frame = tk.Frame(root)
# middle_frame.pack(fill="both", expand=True)

# cpu_label = tk.Label(top_frame, text="CPU Usage: ", font=("Arial", 14))
# cpu_label.pack()

# memory_label = tk.Label(top_frame, text="Memory Usage: ", font=("Arial", 14))
# memory_label.pack()

# process_label = tk.Label(middle_frame, text="Process Table Coming Soon", font=("Arial", 12))
# process_label.pack()

# def update_data():
#     cpu = ps.cpu_percent(interval = None)
#     memory = ps.virtual_memory().percent
#     cpu_label.config(text = f"CPU Usage: {cpu}%")
#     memory_label.config(text=f"Memory Usage: {memory}%")
#     root.after(1000, update_data)
# update_data()

# root.mainloop()



root = tk.Tk()
root.title("Real-Time OS Monitor")
root.geometry("800x500")

#System stacks

top_frame = tk.Frame(root)
top_frame.pack(fill="x", pady=10)
cpu_label = tk.Label(top_frame, text ="CPU Usage: ", font=("Arial", 14))
cpu_label.pack()
memory_label = tk.Label(top_frame, text ="Memory Usage: ", font= ("Arial", 14))
memory_label.pack()
middle_frame = tk.Frame(root)
middle_frame.pack(fill="both", expand = True)
columns = ("PID", "Name", "CPU %", "Memory %", "Status")
tree = ttk.Treeview(middle_frame, columns=columns, show="headings")
for col in columns:
    tree.heading(col,text=col)
    tree.column(col,width=100)
tree.pack(fill="both", expand = True)
scrollbar = ttk.Scrollbar(middle_frame, orient = "vertical", command=tree.yview)
tree.configure(yscroll = scrollbar.set)
scrollbar.pack(side = "right", fill="y")

def update_data():
    cpu = ps.cpu_percent(interval =None)
    memory = ps.virtual_memory().percent
    cpu_label.config(text = f"CPU Usage: {cpu}%")
    memory_label.config(text=f"Memory Usage: {memory}%")
    for row in tree.get_children():
        tree.delete(row)
    processes = []
    for proc in ps.process_iter(['pid','name','cpu_percent','memory_percent','status']):
        try:
            processes.append(proc.info)
        except:
            pass
    for proc in processes[:15]:
        tree.insert("", "end", values=(
            proc['pid'],
            proc['name'],
            proc['cpu_percent'],
            round(proc['memory_percent'], 2),
            proc['status']
        ))
    root.after(1000, update_data)
update_data()
root.mainloop()
