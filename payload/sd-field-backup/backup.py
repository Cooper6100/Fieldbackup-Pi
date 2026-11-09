#!/usr/bin/env python3
import fcntl
"""Read-only SD-card importer, SHA-256 verified, for a Pi booted from NVMe."""
import hashlib, json, logging, os, re, shutil, signal, subprocess, time
from pathlib import Path

CONFIG = Path('/etc/sd-field-backup.json')
STOP = False

def stop(*_):
    global STOP
    STOP = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('sd-field-backup')

def cmd(args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check)

def usb_id(dev):
    node = (Path('/sys/class/block') / Path(dev).name).resolve()
    for parent in (node, *node.parents):
        vendor, product = parent/'idVendor', parent/'idProduct'
        if vendor.exists() and product.exists():
            return f'{vendor.read_text().strip().lower()}:{product.read_text().strip().lower()}'
    return None

def partitions(cfg):
    """Find USB FAT/exFAT volumes (partitions or whole-disk filesystems)."""
    data = json.loads(cmd(['lsblk','-J','-p','-o','NAME,TYPE,FSTYPE,MOUNTPOINT,UUID,TRAN']).stdout)
    result = {}
    def visit(node, disk=None):
        if node.get('type') == 'disk':
            disk = node
        if disk and disk.get('tran') == 'usb' and node.get('type') in ('part','disk'):
            if (node.get('fstype') or '').lower() in ('exfat','vfat','fat','fat32'):
                result[node['name']] = node
        for child in node.get('children', []):
            visit(child, disk)
    for node in data.get('blockdevices', []):
        visit(node)
    return result

def sha256(path):
    h = hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''): h.update(block)
    return h.hexdigest()

def scan_files(source):
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(root)/d).is_symlink())
        for name in sorted(files):
            if name.startswith('.'): continue
            p = Path(root)/name
            if p.is_file() and not p.is_symlink(): yield p

def copy_verified(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_hash = sha256(src)
    if dst.exists():
        if dst.is_file() and not dst.is_symlink() and sha256(dst) == src_hash: return 'SKIPPED'
        raise RuntimeError(f'Conflicting existing destination: {dst}')
    if shutil.disk_usage(dst.parent).free < src.stat().st_size + 15*1024**3:
        raise RuntimeError('Insufficient free space; reserve 15 GiB')
    temp = dst.with_name(dst.name+'.partial')
    if temp.exists():
        if temp.is_symlink() or not temp.is_file(): raise RuntimeError(f'Unsafe partial file: {temp}')
        temp.unlink()
    try:
        with open(src,'rb') as reader, open(temp,'xb') as writer:
            shutil.copyfileobj(reader, writer, 4*1024*1024)
            writer.flush(); os.fsync(writer.fileno())
        if sha256(temp) != src_hash: raise RuntimeError(f'SHA-256 mismatch: {src}')
        os.replace(temp,dst)
        fd = os.open(dst.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return 'COPIED'
    finally:
        if temp.exists(): temp.unlink()

class Lights:
    def __init__(self, cfg):
        self.leds = {}
        try:
            from gpiozero import LED
            for color, pin in cfg.get('gpio_pins',{}).items(): self.leds[color] = LED(int(pin))
        except Exception as e:
            log.warning('GPIO LEDs unavailable: %s',e)
            self.leds = {}
    def show(self, state):
        for led in self.leds.values(): led.off()
        if state == 'copying' and 'blue' in self.leds:
            self.leds['blue'].blink(on_time=.3,off_time=.3,background=True)
        elif state in self.leds: self.leds[state].on()
    def close(self):
        for led in self.leds.values(): led.close()

def check_destination(cfg):
    dest = Path(cfg['destination'])
    expected_uuid = cfg['root_uuid']
    actual_uuid = cmd(['findmnt','-n','-o','UUID','-T',str(dest)]).stdout.strip()
    root_uuid = cmd(['findmnt','-n','-o','UUID','-T','/']).stdout.strip()
    root_source = cmd(['findmnt','-n','-o','SOURCE','-T','/']).stdout.strip()
    if not dest.is_dir() or dest.is_symlink(): raise RuntimeError('Backup directory missing or unsafe')
    if actual_uuid != expected_uuid or root_uuid != expected_uuid or not root_source.startswith('/dev/nvme'):
        raise RuntimeError(f'Backup destination is not the configured NVMe OS filesystem: {root_source}')
    if shutil.disk_usage(dest).free < 15*1024**3: raise RuntimeError('Less than 15 GiB free')
    return dest

def import_card(dev, part, cfg, lights):
    dest = check_destination(cfg)
    if part.get('mountpoint'):
        source = Path(part['mountpoint'])
        opts = cmd(['findmnt','-n','-o','OPTIONS','--target',str(source)]).stdout.strip().split(',')
        if 'ro' not in opts: raise RuntimeError(f'{dev} already mounted read-write at {source}')
        owned = False
    else:
        source = Path('/run/sd-field-backup')/Path(dev).name
        source.mkdir(parents=True,exist_ok=True)
        cmd(['mount','-o','ro,nosuid,nodev,noexec',dev,str(source)])
        owned = True
    success = False
    try:
        card_id = re.sub(r'[^A-Za-z0-9._-]','_',part.get('uuid') or (usb_id(dev) or Path(dev).name))[:80]
        from datetime import datetime
        dcim = source / 'DCIM'
        if not dcim.is_dir() or dcim.is_symlink():
            raise RuntimeError(f'DCIM folder not found on {dev}')

        backup_date = datetime.now().strftime('%Y-%m-%d')

        if usb_id(dev) == '2ca3:0020':
            target = dest / 'DJI-Neo-2'
        else:
            target = dest / backup_date / card_id

        target.mkdir(parents=True, exist_ok=True)
        log.info('Starting DCIM backup %s -> %s', dev, target)
        lights.show('copying')
        copied = skipped = 0
        for src in scan_files(dcim):
            if STOP: raise RuntimeError('Stopped during import')
            rel = src.relative_to(dcim)
            status = copy_verified(src,target/rel)
            copied += status == 'COPIED'; skipped += status == 'SKIPPED'
            log.info('%s %s',status,rel)
        os.sync()
        log.info('Verified backup of %s: %d copied, %d already identical',dev,copied,skipped)
        success = True
    finally:
        if owned:
            try:
                cmd(['umount',str(source)])
                log.info('Safely unmounted %s',dev)
            except Exception as e:
                success = False
                log.error('Could not unmount %s: %s',dev,e)
    if not success: raise RuntimeError('Backup did not finish safely')
    lights.show('green')

def main():
    cfg = json.loads(CONFIG.read_text())
    lights = Lights(cfg)
    seen = set()
    log.info('Field backup started; readers=%s destination=%s',cfg['reader_ids'],cfg['destination'])
    try:
        while not STOP:
            try:
                found = partitions(cfg)
                active = set(found)
                seen &= active
                for dev in sorted(active-seen):
                    seen.add(dev)
                    try:
                        with open('/run/lock/fieldbackup-transfer.lock', 'a') as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            import_card(dev,found[dev],cfg,lights)
                    except Exception as e:
                        log.exception('Backup failed for %s: %s',dev,e)
                        lights.show('red')
            except Exception as e:
                log.exception('Detection error: %s',e)
                lights.show('red')
            time.sleep(3)
    finally: lights.close()
if __name__ == '__main__': main()
