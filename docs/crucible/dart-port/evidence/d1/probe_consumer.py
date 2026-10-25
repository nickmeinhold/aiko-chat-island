import aiko_services as aiko
CHAT = "aiko/4b4281a12660/8/1"; CHANNELS = "aiko/4b4281a12660/8/3"
class ProbeActor(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.c0 = {}; self.c1 = {}
        self.e0 = aiko.ECConsumer(self, 0, self.c0, f"{CHAT}/control", filter="channel_list")
        self.e0.add_handler(self.h)
        
        
    def h(self, cid, command, name, value):
        cache = self.c0.get("channel_list") if cid == 0 else self.c1.get("entries")
        print("HANDLER", cid, command, name, repr(value)[:100], "| keys:", sorted((cache or {}).keys()), flush=True)
p = aiko.compose_instance(ProbeActor, aiko.actor_args("d1probe_consumer", protocol="d1probe:0"))
aiko.process.run()
