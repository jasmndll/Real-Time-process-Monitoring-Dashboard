import psutil as ps
import time
while True:
    print("Top 5 processes by CPU usage\n")
    processes = []
    for proc in ps.process_iter(['pid','name','cpu_percent']):
        processes.append(proc.info)
    processes = sorted(processes, key = lambda x: x['cpu_percent'], reverse = True)
    for process in processes[:5]:
        print(process)
    cpu = ps.cpu_percent(interval =1)
    memory = ps.virtual_memory().percent
    print(f"CPU usage: {cpu}%")
    print(f"Memory usage: {memory}%")
    print("-"*40)
    time.sleep(2)
