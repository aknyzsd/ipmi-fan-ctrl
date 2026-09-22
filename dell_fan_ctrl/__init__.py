"""Dell 服务器风扇 PID 温控（重写版）。

通过 IPMI over LAN 连 iDRAC/BMC，读温度/负载/功耗，
PID + 负载前馈 + 进排风温差前馈，输出手动风扇 PWM。
"""
