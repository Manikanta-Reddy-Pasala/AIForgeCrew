# Pinning the NUC + Mac Studio to fixed IPs

The IP keeps changing because both boxes use **DHCP** — the router hands a new
lease on each reconnect (and when you move between the `.70` and `.78` APs/VLANs).
Pick ONE of the two approaches below. **DHCP reservation is recommended** — it
survives OS reinstalls and doesn't risk a subnet mismatch.

## Option A — DHCP reservation (recommended, do it on the router)
No changes on the boxes. On your router admin page:
1. Find each box's **MAC address**:
   - NUC (Ubuntu):    `ip link`  → the `link/ether` under the LAN interface
   - Mac Studio:      `networksetup -getmacaddress Ethernet` (or Wi-Fi)
2. Add a **DHCP reservation / static lease**: MAC → the IP you want
   (e.g. NUC `192.168.70.115`, Mac Studio `192.168.70.116`).
3. Reboot the box (or renew its lease). It now always gets that IP.
> If the router itself hands out `.78` sometimes, you have two DHCP scopes /
> two APs — reserve on BOTH, or disable one, so the box lands on one subnet.

## Option B — Static IP on the box (self-contained, no router access)
Run the matching script ON the box (needs sudo). Parameterised — pass the
desired IP / gateway / interface. Pick an IP OUTSIDE the DHCP pool to avoid
collisions.

- NUC (Ubuntu / netplan):   `sudo scripts/net/set-static-ip-nuc.sh`
- Mac Studio (macOS):       `sudo scripts/net/set-static-ip-mac-studio.sh`

Both default to `192.168.70.115` (NUC) / `192.168.70.116` (MS), gateway
`192.168.70.1`, DNS `192.168.70.1,1.1.1.1` — override via env (see each script).

After pinning, update `AIFORGE_NUC_HOST` / `AIFORGE_MS_HOST` (or your deploy
env) to the fixed IPs and they'll never drift again.
