import io, sys
sys.stdout.reconfigure(encoding="utf-8")
lines = io.open("workbench_server.py", encoding="utf-8").read().split("\n")
for i, l in enumerate(lines[1262:1300], 1263):
    print("%d: %s" % (i, l))