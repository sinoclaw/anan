#!/usr/bin/env python3
"""
Watch state.db for writes by polling gateway's /proc/PID/fd.
Unlike inotify, this detects existing open file descriptors.
"""
import os, time, subprocess

DB_PATH = "/root/.anan/state.db"
WAL_PATH = "/root/.anan/state.db-wal"
SHM_PATH = "/root/.anan/state.db-shm"
BAK_PATH = "/root/.anan/state.db.corrupted.bak"
GATEWAY_PID = 658502

log_path = "/root/.anan/logs/state_db_watcher.log"

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_path, "a") as f:
        f.write(line + "\n")

def get_fd_info(pid, path_fragment):
    """Get details of open fd for a pid."""
    try:
        result = subprocess.run(
            ["sh", "-c",
             f"for fd in /proc/{pid}/fd/*; do "
             f"link=$(readlink $fd 2>/dev/null); "
             f"[ -n \"$link\" ] && echo \"$link\" | grep -q '{path_fragment}' && "
             f"echo \"fd=$(basename $fd) mode=$link\"; done 2>/dev/null | sort -u"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or "no fd"
    except:
        return "error"

def poll():
    prev_db_size = None
    prev_wal_size = None
    prev_db_mtime = None
    
    log(f"=== Watch started for PID {GATEWAY_PID} ===")
    
    while True:
        try:
            # Check DB size and mtime
            db_stat = os.stat(DB_PATH)
            wal_stat = os.stat(WAL_PATH)
            
            db_size = db_stat.st_size
            wal_size = wal_stat.st_size
            db_mtime = db_stat.st_mtime_ns
            
            db_changed = (prev_db_size is not None and 
                         (db_size != prev_db_size or db_mtime != prev_db_mtime))
            wal_changed = (prev_wal_size is not None and wal_size != prev_wal_size)
            
            if db_changed:
                fd_info = get_fd_info(GATEWAY_PID, "state.db$'")
                log(f"WRITE DB size={db_size:>12} was={prev_db_size:>12} mtime_ns={db_mtime} fd_info={fd_info}")
                # Check header after write
                try:
                    with open(DB_PATH, "rb") as f:
                        hdr = f.read(64)
                    page_size = int.from_bytes(hdr[52:56], 'little')
                    schema_cookie = int.from_bytes(hdr[40:44], 'little')
                    log(f"  header_check: page_size={page_size} schema_cookie={schema_cookie}")
                except Exception as e:
                    log(f"  header_check ERROR: {e}")
            
            if wal_changed:
                log(f"WRITE WAL size={wal_size:>12} was={prev_wal_size:>12}")
            
            prev_db_size = db_size
            prev_wal_size = wal_size
            prev_db_mtime = db_mtime
            
        except FileNotFoundError as e:
            log(f"FILE NOT FOUND: {e}")
        except Exception as e:
            log(f"POLL ERROR: {e}")
        
        time.sleep(2)  # Poll every 2 seconds

if __name__ == "__main__":
    try:
        poll()
    except KeyboardInterrupt:
        print("Stopped.")
