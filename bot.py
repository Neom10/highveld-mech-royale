from array import array
import heapq
import json
import math
from pathlib import Path

import mechfight

TAU = math.tau
DIRECTIONS = [(math.cos(i * TAU / 24), math.sin(i * TAU / 24)) for i in range(24)]
PRIORITIES = "gun_mag gun_damage plating armor lightning_range gun_rate nanites lightning_efficiency laser_damage servos reactor jets".split()
LOOT = {"laser": 2, "lightning": 3, "mortar": 1, "gun": 0}


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_error(a, b):
    return abs(math.remainder(a - b, TAU))


class Arena:
    """Four-metre cells with vertical spans for buildings and bridges."""
    def __init__(self, directory):
        world = json.loads((Path(directory) / "v2/world.json").read_text())
        self.bounds = world["bounds"]
        self.terrain = world["terrain"]
        self.heights = array("h")
        with (Path(directory) / "v2/terrain.i16").open("rb") as terrain_file:
            self.heights.fromfile(terrain_file,self.terrain["nx"]*self.terrain["ny"])
        self.cells = {}
        for prism in world["prisms"]:
            rings = [prism["ring"], *prism["holes"]]
            edges = []
            for flat in rings:
                points = list(zip(flat[::2], flat[1::2]))
                edges.extend(zip(points, points[1:] + points[:1]))
            ys = prism["ring"][1::2]
            for row in range(math.floor(min(ys)/4), math.ceil(max(ys)/4)):
                y = 4 * row + 2
                xs = sorted(x1 + (y-y1)*(x2-x1)/(y2-y1)
                            for (x1,y1),(x2,y2) in edges if (y1 > y) != (y2 > y))
                for left, right in zip(xs[::2], xs[1::2]):
                    for col in range(math.ceil((left-2)/4), math.ceil((right-2)/4)):
                        self.cells.setdefault((col,row), []).append((prism["base"],prism["top"]))

        # Cache traversable ground nodes once, while the hello budget is active.
        self.walkable = set()
        b = self.bounds
        for i in range(math.ceil((b["minX"]+3)/8),math.floor((b["maxX"]-3)/8)+1):
            for j in range(math.ceil((b["minY"]+3)/8),math.floor((b["maxY"]-3)/8)+1):
                if all(self.open_ground(i*8+dx,j*8+dy) for dx,dy in
                       ((0,0),(-2,-2),(-2,2),(2,-2),(2,2))):
                    self.walkable.add((i,j))

    def ground(self,x,y):
        t = self.terrain
        i = min(t["nx"]-1,max(0,round((x-t["minX"])/t["step"])))
        j = min(t["ny"]-1,max(0,round((y-t["minY"])/t["step"])))
        return self.heights[j*t["nx"]+i]/100

    def open_ground(self,x,y):
        z = self.ground(x,y)
        return not self.solid(x,y,z+1.2,z+9)

    def route(self,pos,goal):
        def snap(point):
            i,j = round(point[0]/8),round(point[1]/8)
            return min(((i+u,j+v) for u in range(-2,3) for v in range(-2,3)
                        if (i+u,j+v) in self.walkable),
                       key=lambda n: distance((8*n[0],8*n[1]),point),default=None)
        start,end = snap(pos),snap(goal)
        if start is None or end is None:
            return []
        frontier = [(distance(start,end),0,start)]
        parent,cost = {start:None},{start:0}
        for _ in range(1200):
            if not frontier:
                return []
            _,g,node = heapq.heappop(frontier)
            if node == end:
                path = []
                while parent[node] is not None:
                    path.append((node[0]*8,node[1]*8))
                    node = parent[node]
                return path[::-1]
            if g != cost[node]:
                continue
            i,j = node
            for u,v in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,1),(1,-1),(-1,-1)):
                n = (i+u,j+v)
                if n not in self.walkable or (u and v and
                        ((i+u,j) not in self.walkable or (i,j+v) not in self.walkable)):
                    continue
                ng = g+(1.4143 if u and v else 1)
                if ng < cost.get(n,math.inf):
                    cost[n],parent[n] = ng,node
                    heapq.heappush(frontier,(ng+distance(n,end),ng,n))
        return []

    def solid(self, x, y, low, high):
        return any(base < high and top > low for base, top in
                   self.cells.get((math.floor(x/4), math.floor(y/4)), ()))

    def clear(self, a, b):
        steps = max(1, math.ceil(distance(a, b)/4))
        for i in range(1, steps):
            f = i/steps
            x, y, z = (u+(v-u)*f for u,v in zip(a,b))
            if self.solid(x,y,z-0.1,z+0.1):
                return False
        return True


class Bot:
    name = "Highveld III"

    def __init__(self, hello):
        self.map = Arena(hello["map"]["dir"])
        self.cfg = hello["config"]
        self.phase = hello["slot"] * 1.7
        self.heading = (0,0)
        self.last_plan = -10
        self.last_pos = None
        self.jet_until = -10
        self.last_bearing = None
        self.heard_at = -10
        self.route_goal = None
        self.route_nodes = []
        self.route_time = -10
        self.focus = None
        self.recovering = False
        self.loot_memory = {}
        self.last_enemy_pos = {}
        self.reward_pos = None
        self.reward_until = 0

    def move(self, obs, goal, threats, retreat, duel):
        me, zone = obs["self"], obs["zone"]
        x,y,z = me["pos"]
        now = obs["t"]
        if now - self.last_plan >= 0.29:
            b = self.map.bounds
            best = -math.inf
            near_center = distance((x,y),goal) < 15
            headings = [(0,0), *DIRECTIONS]
            if near_center:
                headings.append(((goal[0]-x)/14,(goal[1]-y)/14))
            for dx,dy in headings:
                q = (x+dx*14, y+dy*14)
                score = distance((x,y),goal) - distance(q,goal)
                score += 1.6*(dx*self.heading[0] + dy*self.heading[1])
                score -= 4*max(0, distance(q,zone["center"]) - max(0,zone["radius"]-4))
                if not (b["minX"]+3 < q[0] < b["maxX"]-3 and b["minY"]+3 < q[1] < b["maxY"]-3):
                    score -= 120
                for step in (4,9,14):
                    if self.map.solid(x+dx*step,y+dy*step,z+1.3,z+8.7):
                        score -= (18-step)*1.9
                        break
                for enemy in threats[:3]:
                    p = enemy["pos"]
                    dist = max(1,distance((x,y),p))
                    outward = (dx*(x-p[0])+dy*(y-p[1]))/dist
                    side = (dx*(p[1]-y)-dy*(p[0]-x))/dist
                    score += outward * (13 if retreat else 1)
                    lateral = 9 if duel else 6
                    score += side * (lateral if math.sin(now*1.2+self.phase)>0 else -lateral)
                    if retreat and not self.map.clear((*q,z+6), (p[0],p[1],p[2]+7)):
                        score += 13
                if score > best:
                    best, heading = score,(dx,dy)
            self.heading = heading
            if self.last_pos and distance((x,y),self.last_pos)<0.8 and distance((x,y),goal)>14 and me["energy"]>35:
                self.jet_until = now + 2.4
            self.last_pos = x,y
            self.last_plan = now
        jet = now < self.jet_until and me["energy"]>5
        return {"move":self.heading,"jump":jet and me["grounded"],
                "boost":jet or (me["grounded"] and me["energy"]>45 and (retreat or not me["in_zone"]))}

    def act(self, obs):
        me, zone, now = obs["self"],obs["zone"],obs["t"]
        pos = me["pos"]
        x,y,z = pos
        muzzle = (x,y,z+me["stats"]["muzzle_height"])
        enemies = sorted((e for e in obs["mechs"] if e["kind"]=="mech"),
                         key=lambda e:distance(pos,e["pos"]))
        health = me["hp"]/me["max_hp"]
        if health < 0.53:
            self.recovering = True
        elif health > 0.85:
            self.recovering = False
        visible = [e for e in enemies if self.map.clear(muzzle, (*e["pos"][:2],e["pos"][2]+5))]
        for e in enemies:
            self.last_enemy_pos[e["id"]] = (e["pos"],now)
        for ev in obs["events"]:
            if ev["type"] == "kill" and ev["target"] in self.last_enemy_pos:
                point,seen = self.last_enemy_pos[ev["target"]]
                if now-seen < 2:
                    self.reward_pos, self.reward_until = point,now+9
        local_pressure = sum(distance(pos,e["pos"]) < 120 for e in visible)
        def duel_cost(e):
            return (distance(pos,e["pos"]) + 0.11*e.get("hp",1000)
                    - (30 if e["id"] == self.focus else 0))
        rival = min(visible,key=duel_cost,default=None)
        finish = rival is not None and rival.get("hp",1000) <= 0.22*me["hp"]
        retreat = local_pressure >= 3 or (local_pressure >= 2 and not finish) or (self.recovering and not finish)
        duel = rival is not None and not retreat
        sec = me["weapons"][1]
        kind = sec["kind"] if sec else "gun"
        gun = me["weapons"][0]
        action = {"tick":obs["tick"]}
        if obs.get("offer"):
            choices = obs["offer"]["choices"]
            action["pick"] = min(range(len(choices)),key=lambda i:
                PRIORITIES.index(choices[i]["id"]) if choices[i]["id"] in PRIORITIES else 99)
        goal = zone["next_center"]
        options = []
        # Cubes and weapons disappear from the sensors when the torso turns.
        visible_ids = {item["id"] for item in (*obs["cubes"], *obs["pickups"])}
        for key,(item,seen) in list(self.loot_memory.items()):
            if now-seen > 12 or (key not in visible_ids and math.dist(pos,item["pos"]) < 7):
                del self.loot_memory[key]
        for item in (*obs["cubes"], *obs["pickups"]):
            self.loot_memory[item["id"]] = (item,now)
        best_take = (0,-math.inf)
        for p,_ in self.loot_memory.values():
            if abs(p["pos"][2]-z) > 9:
                continue
            if "kind" not in p:
                options.append((math.dist(pos,p["pos"])-50,p["pos"]))
                continue
            gain = LOOT.get(p["kind"],0)-LOOT[kind]
            if gain<=0:
                continue
            d = math.dist(pos,p["pos"])
            if p["id"] in visible_ids and d < self.cfg["loot"]["pickup_radius"] and (gain,-d)>best_take:
                best_take = gain,-d
                action["take"] = p["id"]
                if sec:
                    action["drop"] = 1
            options.append((d-27*gain,p["pos"]))

        options.extend((math.dist(pos,c["pos"])+6,c["pos"]) for c in obs["crates"])
        for drop in obs["drops"]:
            d = distance(pos,drop["pos"])
            if drop["lands_in"] < d/me["stats"]["walk_speed"]+6 and not enemies:
                options.append((d-95,drop["pos"]))
        if options:
            cost, item = min(options,key=lambda o:o[0])
            if cost<90:
                goal = item
        if self.reward_pos and now < self.reward_until and not retreat:
            if distance(pos,self.reward_pos) < 8 or obs["cubes"]:
                self.reward_pos = None
            else:
                goal = self.reward_pos
        if duel:
            # Set the radius of the orbit from the equipped game ability.
            nearest = rival["pos"]
            gap = max(1,distance(pos,nearest))
            desired = 0.70*sec["stats"]["range"] if kind == "lightning" else 58
            if kind == "laser":
                desired = 95
            gear = [p for p in obs["pickups"] if LOOT.get(p["kind"],0)>LOOT[kind]
                    and math.dist(pos,p["pos"]) < 20 and abs(p["pos"][2]-z)<4]
            if gear:
                goal = min(gear,key=lambda p:math.dist(pos,p["pos"]))["pos"]
            else:
                ux,uy = (x-nearest[0])/gap,(y-nearest[1])/gap
                tangent = 13 if math.sin(now*1.2+self.phase)>0 else -13
                goal = (nearest[0]+ux*desired-uy*tangent,
                        nearest[1]+uy*desired+ux*tangent)
        if enemies and retreat:
            e = enemies[0]["pos"]
            d = max(1,distance(pos,e))
            goal = (x+30*(x-e[0])/d,y+30*(y-e[1])/d)
        # Leave time for walls and prefer the destination circle during shrink.
        center = zone["next_center"]
        excess = distance(pos,center)-zone["next_radius"]
        if (not me["in_zone"] or zone["radius"]<35 or
                (excess>-5 and zone["shrink_end"]-now < max(8,excess/6+16))):
            goal = center
            if zone["radius"]<35:
                retreat = False
        threats = enemies if zone["radius"]>10 else []
        if (not threats or not me["in_zone"]) and distance(pos,goal)>16 and abs(z-self.map.ground(x,y))<4:
            if self.route_goal is None or distance(self.route_goal,goal)>10:
                self.route_nodes = []
                self.route_goal = goal
            if now-self.route_time > 2 and not self.route_nodes:
                self.route_time = now
                if not self.map.clear(muzzle,(goal[0],goal[1],self.map.ground(*goal[:2])+6)):
                    self.route_nodes = self.map.route(pos,goal)
            while self.route_nodes and distance(pos,self.route_nodes[0])<5:
                self.route_nodes.pop(0)
            if self.route_nodes:
                goal = self.route_nodes[0]
        else:
            self.route_nodes = []
        action.update(self.move(obs,goal,threats,retreat,duel))
        for event in [*obs["sounds"],*obs["events"]]:
            if event.get("bearing") is not None and (event.get("type")=="damaged" or event.get("dist") in ("near","mid")):
                self.last_bearing, self.heard_at = event["bearing"],now
        yaw = self.last_bearing if now-self.heard_at<1.8 else math.remainder(now*1.55+self.phase,TAU)
        action.update(aim=[yaw,0],fire=[False,False])
        candidates = []
        for target in [*obs["mechs"],*obs["crates"]]:
            typ = target.get("kind","crate")
            p = target["pos"]
            height = {"mech":4.5,"npc":1.75,"crate":1.2}[typ]
            dst = (p[0],p[1],p[2]+height)
            d = math.dist(muzzle,dst)
            if d > max(gun["stats"]["range"],sec["stats"].get("range",0) if sec else 0):
                continue
            cost = d + 0.062*target.get("hp",1000) + {"mech":0,"npc":95,"crate":108}[typ]
            if target["id"] == self.focus:
                cost -= 32
            if typ == "mech":
                cost -= 32
            if duel and target["id"] == rival["id"]:
                cost -= 350
            candidates.append((cost,target,dst,d))
        for _,target,dst,d in sorted(candidates,key=lambda c:c[0]):
            if not self.map.clear(muzzle,dst):
                continue
            # Dealing damage restarts the game's regeneration delay.
            if self.recovering and not finish and zone["radius"] > 30 and (
                    target.get("kind") != "mech" or
                    (d > 85 and target.get("hp",1000) > 180)):
                continue
            if retreat and health < 0.65 and target.get("kind")!="mech":
                continue
            self.focus = target["id"]
            vx,vy,vz = target.get("vel",(0,0,0))
            flight = d/gun["stats"]["speed"]
            laser = kind=="laser" and d<sec["stats"]["range"] and sec["cooldown"]<=0.1
            mortar = kind=="mortar" and 15<d<130 and sec["cooldown"]<=0.05
            if laser:
                flight = 0
            if mortar:
                flight = d/sec["stats"]["speed"]
            ax,ay,az = (dst[0]-x+vx*flight,dst[1]-y+vy*flight,dst[2]-muzzle[2]+vz*flight)
            if mortar:
                az += 0.5*sec["stats"]["gravity"]*flight**2-2.5
            yaw,pitch = math.atan2(ay,ax),math.atan2(az,math.hypot(ax,ay))
            aligned = (angle_error(yaw,me["aim"][0]) < 0.10+2/max(5,d)
                       and abs(pitch-me["aim"][1])<0.10+2/max(5,d))
            action["aim"] = [yaw,max(me["stats"]["min_pitch"],min(me["stats"]["max_pitch"],pitch))]
            action["fire"][0] = aligned and d<=gun["stats"]["range"] and not mortar
            if laser:
                action["fire"][1] = sec["charge"] < sec["stats"]["charge"] or not aligned
            elif kind=="lightning":
                action["fire"][1] = (d < sec["stats"]["range"] and
                                     angle_error(me["aim"][0],yaw) < math.radians(sec["stats"]["cone_deg"]) and
                                     me["energy"] >= sec["stats"]["energy"])
            elif mortar:
                action["fire"][1] = aligned
            break
        else:
            action["reload"] = gun["ammo"] < gun["stats"]["mag"]*0.8
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
