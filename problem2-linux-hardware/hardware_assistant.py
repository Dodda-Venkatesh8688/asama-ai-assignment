import json
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

TOOL_REGISTRY = {
    "get_cpu_info": {
        "cmd": ["lscpu"],
        "binary": "lscpu",
        "description": "Get CPU model, architecture, physical cores, threads, and clock speeds.",
    },
    "get_memory_summary": {
        "cmd": ["free", "-b"],
        "binary": "free",
        "description": "Get currently installed total, used, and available RAM in bytes.",
    },
    "get_dmi_memory": {
        "cmd": ["sudo", "-n", "/usr/sbin/dmidecode", "-t", "memory"],
        "binary": "dmidecode",
        "description": "Get physical RAM slots, installed stick sizes, and maximum motherboard capacity.",
    },
    "get_storage_devices": {
        "cmd": ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,MODEL,ROTA,TRAN"],
        "binary": "lsblk",
        "description": "Get storage drives, partitions, sizes, and bus transport (NVMe vs SATA).",
    },
    "get_gpu_pci_devices": {
        "cmd": ["lspci"],
        "binary": "lspci",
        "description": "Get GPU, graphics controller, and PCI hardware devices.",
    },
}


def parse_tool_output(tool_name: str, stdout: str) -> Dict[str, Any]:
    if tool_name == "get_storage_devices":
        return json.loads(stdout)

    if tool_name == "get_cpu_info":
        data = {}
        for line in stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        return {
            "model_name": data.get("Model name", "Unknown"),
            "architecture": data.get("Architecture", "Unknown"),
            "cpus": data.get("CPU(s)", "Unknown"),
            "cores_per_socket": data.get("Core(s) per socket", "Unknown"),
            "threads_per_core": data.get("Thread(s) per core", "Unknown"),
        }

    if tool_name == "get_memory_summary":
        for line in stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                total_gb = round(int(parts[1]) / (1024 ** 3), 2)
                used_gb = round(int(parts[2]) / (1024 ** 3), 2)
                avail_gb = round(int(parts[6]) / (1024 ** 3), 2)
                return {
                    "total_ram_gb": total_gb,
                    "used_ram_gb": used_gb,
                    "available_ram_gb": avail_gb,
                }
        return {"error": "Could not parse free output"}

    if tool_name == "get_dmi_memory":
        max_cap = re.search(r"Maximum Capacity:\s*(.+)", stdout)
        sizes = re.findall(r"^\s*Size:\s*(.+)$", stdout, re.MULTILINE)
        installed = [s.strip() for s in sizes if "No Module Installed" not in s and "None" not in s]
        return {
            "maximum_supported_capacity": max_cap.group(1).strip() if max_cap else "Unknown",
            "total_slots": len(sizes),
            "used_slots": len(installed),
            "free_slots": max(0, len(sizes) - len(installed)),
            "installed_modules": installed,
        }

    if tool_name == "get_gpu_pci_devices":
        gpus = [
            line.strip()
            for line in stdout.splitlines()
            if any(tag in line.lower() for tag in ["vga", "3d controller", "display controller"])
        ]
        return {"graphics_controllers": gpus}

    return {"raw_output": stdout}


def run_hardware_tool(tool_name: str) -> Dict[str, Any]:
    if tool_name not in TOOL_REGISTRY:
        return {"status": "error", "reason": f"Blocked: '{tool_name}' is not in allow-list."}

    if platform.system() != "Linux":
        return {
            "status": "unavailable",
            "reason": f"Host OS is {platform.system()}. Linux utilities ({TOOL_REGISTRY[tool_name]['binary']}) require a Linux environment.",
        }

    spec = TOOL_REGISTRY[tool_name]
    if not shutil.which(spec["binary"]):
        return {"status": "missing_tool", "reason": f"Binary '{spec['binary']}' is not installed on this distro."}

    try:
        proc = subprocess.run(
            spec["cmd"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            shell=False,
        )
        return {"status": "ok", "tool": tool_name, "data": parse_tool_output(tool_name, proc.stdout)}
    except subprocess.CalledProcessError as e:
        if tool_name == "get_dmi_memory":
            fallback = run_hardware_tool("get_memory_summary")
            fallback["note"] = "dmidecode requires scoped sudo permissions; fell back to /proc/meminfo via free."
            return fallback
        return {"status": "error", "reason": e.stderr.strip() or str(e)}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": f"{tool_name} exceeded 5s timeout."}


class HardwareAssistant:
    def __init__(self):
        self.cached_context: Dict[str, Any] = {}
        self.last_topic: Optional[str] = None

    def select_tools(self, query: str) -> List[str]:
        q = query.lower()
        selected = []

        if any(k in q for k in ["ram", "memory", "slot", "upgrade"]):
            selected.extend(["get_memory_summary", "get_dmi_memory"])
            self.last_topic = "memory"
        if any(k in q for k in ["cpu", "processor", "core", "thread"]):
            selected.append("get_cpu_info")
            self.last_topic = "cpu"
        if any(k in q for k in ["ssd", "nvme", "disk", "storage", "drive", "hdd"]):
            selected.append("get_storage_devices")
            self.last_topic = "storage"
        if any(k in q for k in ["gpu", "graphics", "nvidia", "amd", "intel iris", "vga"]):
            selected.append("get_gpu_pci_devices")
            self.last_topic = "gpu"

        if not selected and self.last_topic == "memory" and any(k in q for k in ["how much", "more", "add", "max"]):
            selected.extend(["get_memory_summary", "get_dmi_memory"])

        return list(dict.fromkeys(selected))

    def answer(self, user_query: str) -> str:
        tools = self.select_tools(user_query)
        if not tools:
            return (
                "I can only inspect verified Linux hardware specs (CPU, RAM/slots, Storage/NVMe, GPU). "
                "I cannot guess or access unverified system details."
            )

        results = {}
        for t in tools:
            if t not in self.cached_context:
                self.cached_context[t] = run_hardware_tool(t)
            results[t] = self.cached_context[t]

        responses = []
        for t, res in results.items():
            if res.get("status") != "ok":
                responses.append(f"[{t}] Unavailable: {res.get('reason')}")
                continue

            data = res["data"]
            if t == "get_memory_summary":
                responses.append(
                    f"Installed RAM: {data['total_ram_gb']} GB (Used: {data['used_ram_gb']} GB, Available: {data['available_ram_gb']} GB)."
                )
            elif t == "get_dmi_memory":
                responses.append(
                    f"Slots: {data['used_slots']} of {data['total_slots']} used ({data['free_slots']} free). "
                    f"Modules: {', '.join(data['installed_modules']) or 'Unknown'}. "
                    f"Max board capacity: {data['maximum_supported_capacity']}."
                )
            elif t == "get_cpu_info":
                responses.append(
                    f"CPU: {data['model_name']} ({data['architecture']}), {data['cpus']} logical CPUs "
                    f"({data['cores_per_socket']} cores/socket, {data['threads_per_core']} threads/core)."
                )
            elif t == "get_storage_devices":
                disks = [d for d in data.get("blockdevices", []) if d.get("type") == "disk"]
                for d in disks:
                    bus = (d.get("tran") or "unknown").upper()
                    responses.append(f"Drive /dev/{d['name']}: {d.get('size')} ({d.get('model') or 'Generic'}, Bus: {bus}).")
            elif t == "get_gpu_pci_devices":
                gpus = data.get("graphics_controllers", [])
                responses.append(f"Graphics Controllers: {'; '.join(gpus) if gpus else 'None detected via lspci'}.")

        return "\n".join(responses)


if __name__ == "__main__":
    assistant = HardwareAssistant()
    print("Linux Hardware Q&A Assistant (Type 'exit' to quit)\n")
    while True:
        q = input("You: ").strip()
        if q.lower() in {"exit", "quit"}:
            break
        print(f"Assistant:\n{assistant.answer(q)}\n")