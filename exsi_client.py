import socket
import struct
import time
import threading
import os
import subprocess
import queue
import re


class exsi:
    def __init__(self, ip='10.0.1.1', port=8895, output_file='scanner_output.txt'):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.settimeout(1)  # Set a timeout of 1 second
        self.s.connect((ip, port))
        self.counter = 0
        self.running = True
        self.ready_event = threading.Event()
        self.command_queue = queue.Queue()  # Command queue
        self.output_file = output_file
        self.last_command = ""
        # task queue, if a protocol is loaded so that we can run tasks in order
        self.task_queue = queue.Queue() 

        with open(self.output_file, 'w') as file:
            pass

        self.start_receiving_thread()
        self.start_command_processor_thread()  # Start the command processor thread
        self.send('ConnectToScanner product=newHV passwd=rTpAtD', immediate=True)
        self.send('NotifyEvent all=on')

    def send(self, cmd, immediate=False):
        if immediate:
            # For immediate commands like the initial connection; bypass the queue
            self._send_command(cmd)
        else:
            # check if we are trying to queue the next task without 
            # a task number specified
            if re.search("SelectTask taskkey=(?!\d+)", cmd) is not None:
                # pull the next task from the task list and append
                tasktodo = str(self.task_queue.get())
                self.task_queue.task_done()  
                self.command_queue.put(cmd + tasktodo)
            else: 
                # add command directly to queue if nothing is special
                self.command_queue.put(cmd)

    def _send_command(self, cmd):     
        self.ready_event.clear()
        self.last_command = cmd
        tcmd = b'>heartvista:1:' + str(self.counter).encode('utf-8') + b'>' + cmd.encode('utf-8')
        self.counter += 1
        self.s.send(struct.pack('!H', 16))
        self.s.send(struct.pack('!H', 1000))
        self.s.send(struct.pack('!I', len(tcmd)))
        self.s.send(struct.pack('!I', 0))
        self.s.send(struct.pack('!I', 100))
        self.s.send(tcmd)

    def rcv(self, length=6000):
        data = self.s.recv(length)
        msg = str(data, 'UTF-8', errors='ignore')
        msg = msg[msg.find('<')+1:]
        return msg

    def receive_loop(self):
        while self.running:
            try:
                msg = self.rcv()
                if msg:
                    success, is_ready = self.is_ready(msg)
                    with open(self.output_file, 'a') as file:
                        file.write("Received: " + msg + "\n")
                    if not success:
                        notify = "Command Failed: "
                        notify += self.last_command
                        notify += "\nClearing Command Queue\n\n"
                        with open(self.output_file, 'a') as file:
                            file.write(notify)
                        self.clear_command_queue()  # Clear the queue on failure
                    if is_ready:
                        self.ready_event.set()
            except socket.timeout:
                continue
            except Exception as e:
                with open(self.output_file, 'a') as file:
                    file.write("Error receiving data: " + str(e) + \
                               "\n!!!Please Restart the Client!!!\n")
                break

    def is_ready(self, msg):
        # Return a tuple (success, is_ready)
        
        success = True  # Assume success unless a failure condition is detected
        ready = False 

        if "fail" in msg:
            success = False # Command failed
            ready = True # ready for next command bc we clear the queue
            
        # Check for specific command completion or readiness indicators
        elif self.last_command.startswith("Scan"):
            ready = "acquisition=complete" in msg
        elif self.last_command.startswith("ActivateTask"):
            ready = "ActivateTask=" in msg
        elif self.last_command.startswith("SelectTask"):
            ready = "SelectTask=" in msg
        elif self.last_command.startswith("PatientTable"):
            ready = "PatientTable=" in msg
        elif self.last_command.startswith("LoadProtocol"):
            ready = "LoadProtocol=" in msg
            tasks = msg[msg.find('taskKeys=')+9:-2].split(",")
            for task in tasks:
                self.task_queue.put(task)
        elif self.last_command.startswith("SetCVs"):
            ready = "SetCVs" in msg
        elif self.last_command.startswith("Prescan"):
        ww    ready = "Prescan" in msg
        elif self.last_command.startswith("SetGrxSlices"):
            ready = True
        elif self.last_command.startswith("SetGrxsomething...."):
            ready = True
        elif self.last_command.startswith("Help"):
            ready = "Help" in msg
            
        else:
            # Default condition if none of the above matches
            ready = "NotifyEvent" in msg        # Default readiness condition

        return (success, ready)

    def clear_command_queue(self):
        while not self.command_queue.empty():
            try:
                self.command_queue.get_nowait()
                self.command_queue.task_done()
            except queue.Empty:
                break
        with open(self.output_file, 'a') as file:
            file.write("Command queue cleared due to failure.\n")

    def start_receiving_thread(self):
        self.receiving_thread = threading.Thread(target=self.receive_loop)
        self.receiving_thread.daemon = True
        self.receiving_thread.start()

    def start_command_processor_thread(self):
        def process_commands():
            while self.running:
                try:
                    # Wait for up to 1 second
                    cmd = self.command_queue.get(timeout=1) 
                    self._send_command(cmd)
                    self.wait_for_ready(timeout=60)
                    self.command_queue.task_done()
                except queue.Empty:
                    # Go back to the start of the loop to check self.running again
                    continue

        self.command_processor_thread = threading.Thread(target=process_commands)
        self.command_processor_thread.daemon = True
        self.command_processor_thread.start()

    def wait_for_ready(self, timeout=None):
        ready = self.ready_event.wait(timeout)
        if not ready:
            self.stop()
            raise TimeoutError("Timed out waiting for the MRI machine to be ready.")
        self.ready_event.clear()

    def stop(self):
        self.running = False
        # Insert a dummy command to unblock the command_processor_thread
        self.command_queue.put(None)
        self.receiving_thread.join()
        self.command_processor_thread.join()
        self.s.close()
