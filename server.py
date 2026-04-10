#!/usr/bin/env python3
"""
APL Sandbox Server
Bridges the litegraph frontend to Dyalog APL and Rhino 3D.
Usage: python3 server.py
Then open http://localhost:5000 in a browser.
"""

import json
import os
import shutil
import socket
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─────────────────────────────────────────────
# CONFIG — edit these if auto-detection fails
# ─────────────────────────────────────────────
SERVER_PORT  = 5000
RHINO_HOST   = 'localhost'
RHINO_PORT   = 6789
DYALOG_PATH  = None   # leave None for auto-detection

# ─────────────────────────────────────────────
# Auto-detect Dyalog
# ─────────────────────────────────────────────
def find_dyalog():
    if DYALOG_PATH:
        return DYALOG_PATH
    candidates = [
        shutil.which('dyalogscript'),
        '/usr/bin/dyalogscript',
        '/opt/mdyalog/20.0/64/unicode/dyalogscript',
        '/opt/mdyalog/19.0/64/unicode/dyalogscript',
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

DYALOG = find_dyalog()

# ─────────────────────────────────────────────
# APL evaluation
# ─────────────────────────────────────────────
def evaluate_apl(script):
    """Run an APL script through dyalogscript via temp file."""
    import tempfile
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.apl', delete=False, encoding='utf-8') as f:
            f.write(script)
            tmppath = f.name
        
        proc = subprocess.run(
            ['dyalogscript', tmppath],
            capture_output=True,
            text=True,
            timeout=30,
        )
        os.unlink(tmppath)
        return proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        try: os.unlink(tmppath)
        except: pass
        return None, 'APL evaluation timed out (30s)'
    except Exception as e:
        return None, str(e)

# ─────────────────────────────────────────────
# Graph execution
# ─────────────────────────────────────────────
def execute_graph(graph_data):
    """
    Walk a serialised litegraph graph, build an APL script,
    evaluate it in Dyalog, and return panel results.

    litegraph links format: [id, src_node, src_slot, dst_node, dst_slot, type]
    Node input slots:
        APL/Function  slot 0 = ⍵   slot 1 = ⍺
        APL/Panel     slot 0 = in
        APL/Rhino/*   slot 0 = data
    """
    nodes     = {n['id']: n for n in graph_data.get('nodes', [])}
    links_raw = graph_data.get('links', [])

    # {(dst_node_id, dst_slot): (src_node_id, src_slot)}
    input_map = {}
    for lnk in links_raw:
        if len(lnk) >= 5:
            _, src_n, src_s, dst_n, dst_s = lnk[:5]
            input_map[(dst_n, dst_s)] = (src_n, src_s)

    # litegraph provides an 'order' field for topological sort
    sorted_nodes = sorted(nodes.values(), key=lambda n: n.get('order', 0))

    # Build APL script line by line
    lines = [
    '⎕PP←10',
    '⎕PW←200',
    '',
    ]
    # {(node_id, output_slot): apl_var_name}
    out_vars = {}

    def input_var(nid, slot):
        key = (nid, slot)
        if key in input_map:
            src_n, src_s = input_map[key]
            return out_vars.get((src_n, src_s))
        return None

    panel_ids = []
    rhino_ids = []

    for node in sorted_nodes:
        nid   = node['id']
        ntype = node['type']
        props = node.get('properties', {})

        if ntype == 'APL/Input':
            data = props.get('data', '⍬').strip() or '⍬'
            var  = f'_v{nid}'
            lines.append(f':Trap 0')
            lines.append(f'    {var}←{data}')
            lines.append(f':Else')
            lines.append(f"    {var}←'ERROR: ',⎕DMX.Message")
            lines.append(f':EndTrap')
            out_vars[(nid, 0)] = var

        elif ntype == 'APL/Function':
            expr  = props.get('expr', '⍳').strip() or '⍳'
            omega = input_var(nid, 0)   # ⍵
            alpha = input_var(nid, 1)   # ⍺
            var   = f'_v{nid}'

            lines.append(f':Trap 0')
            if omega and alpha:
                lines.append(f'    {var}←{alpha} ({expr}) {omega}')
            elif omega:
                lines.append(f'    {var}←({expr}) {omega}')
            else:
                lines.append(f"    {var}←'ERROR: ⍵ not connected'")
            lines.append(f':Else')
            lines.append(f"    {var}←'ERROR: ',⎕DMX.Message")
            lines.append(f':EndTrap')
            out_vars[(nid, 0)] = var

        elif ntype == 'APL/Panel':
            src = input_var(nid, 0)
            var = f'_v{nid}'
            if src:
                lines.append(f"{var}←{src}")
                lines.append(f"⎕←'<<<NODE_{nid}>>>'")
                lines.append(f'⎕←{var}')
                lines.append(f"⎕←'<<<END_{nid}>>>'")
                out_vars[(nid, 0)] = var
            panel_ids.append(nid)

        elif ntype == 'APL/Rhino/Point':
            src = input_var(nid, 0)
            var = f'_v{nid}'
            if src:
                lines.append(f"{var}←{src}")
                lines.append(f"⎕←'<<<RHINO_POINT_{nid}>>>'")
                lines.append(f'⎕←{var}')
                lines.append(f"⎕←'<<<END_{nid}>>>'")
                out_vars[(nid, 0)] = var
            rhino_ids.append(nid)

    lines.append('')
    lines.append(')off')

    script = '\n'.join(lines)
    stdout, stderr = evaluate_apl(script)

    if stdout is None:
        return {'error': stderr, 'panels': {}, 'script': script}

    # ── parse output ──────────────────────────────────────────────────
    results  = {'panels': {}, 'rhino_points': [], 'script': script, 'stderr': stderr}
    current  = None
    ctype    = None
    cbuf     = []

    for raw_line in stdout.split('\n'):
        line = raw_line.rstrip()

        # Skip bare APL prompts (6 spaces)
        if line == '      ':
            continue

        if line.startswith('<<<NODE_') and line.endswith('>>>'):
            current = line[8:-3]
            ctype   = 'panel'
            cbuf    = []
        elif line.startswith('<<<RHINO_POINT_') and line.endswith('>>>'):
            current = line[15:-3]
            ctype   = 'rhino'
            cbuf    = []
        elif current and line == f'<<<END_{current}>>>':
            text = '\n'.join(cbuf).strip()
            if ctype == 'panel':
                results['panels'][current] = text
        elif ctype == 'rhino':
            text = '\n'.join(cbuf).strip()
            try:
                # Parse all numbers from the output
                nums = [float(x) for x in text.split()]
                # Group into triples
                if len(nums) >= 3:
                    coords = []
                    for i in range(0, len(nums) - 2, 3):
                        coords.append([nums[i], nums[i+1], nums[i+2]])
                    if len(coords) == 1:
                        results['rhino_points'].append(
                            {'type': 'point', 'x': coords[0][0], 'y': coords[0][1], 'z': coords[0][2]}
                        )
                    else:
                        results['rhino_points'].append(
                            {'type': 'points', 'coords': coords}
                        )
            except Exception:
                pass
            current = None
            cbuf    = []
        elif current:
            cbuf.append(line)

    # ── send geometry to Rhino ────────────────────────────────────────
    for pt in results['rhino_points']:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((RHINO_HOST, RHINO_PORT))
            s.send(json.dumps(pt).encode())
            s.close()
        except Exception:
            pass
    return results


# ─────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log noise

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            path = os.path.join(SCRIPT_DIR, 'index.html')
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()

        elif self.path == '/status':
            status = {
                'dyalog': DYALOG or 'not found',
                'rhino':  f'{RHINO_HOST}:{RHINO_PORT}',
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        if self.path == '/execute':
            result = execute_graph(body)
            payload = json.dumps(result).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == '__main__':
    if not DYALOG:
        print('⚠  Dyalog not found — set DYALOG_PATH at the top of server.py')
    else:
        print(f'✓  Dyalog : {DYALOG}')
    print(f'✓  Rhino  : {RHINO_HOST}:{RHINO_PORT}')
    print(f'→  Open   : http://localhost:{SERVER_PORT}')
    HTTPServer(('localhost', SERVER_PORT), Handler).serve_forever()
