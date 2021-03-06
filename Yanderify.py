# coding: utf-8
print("Loading, please wait a moment until the GUI appears.\n\nDo not close this console! (even if the GUI is active)\n")

import warnings
import sys

if 'debug' not in sys.argv:
    warnings.simplefilter('ignore')

progress = 0
progress_max = 0
stopped = True

try:
    from nosteg import ffmpeg_flags
except ImportError:
    ffmpeg_flags = False

import matplotlib
matplotlib.use('Agg')
from tkinter import *
from tkinter import filedialog
from tkinter import scrolledtext
from tkinter.ttk import *
import os
import queue
import shlex
import shutil
import subprocess
import threading
import traceback
import imageio
imageio.plugins.ffmpeg.download()
import torch
from skimage import img_as_ubyte, img_as_float, exposure
import skimage.transform as transform
import cv2
import numpy as np
import webbrowser
sys.path.append('fomm/')
from demo import *


#print('Loading checkpoints...')

checkpoints = {
    'cpu': True
}

def reload():
    with warnings.catch_warnings():
        global checkpoints
        warnings.simplefilter('ignore')
        demo_g, demo_kp = load_checkpoints('vox-256.yaml', 'checkpoint.tar', cpu=checkpoints['cpu'])
        checkpoints['g'] = demo_g
        checkpoints['kp'] = demo_kp

reload()

#print('Initializing windows...')

root = Tk()
use_cpu = IntVar()
st = None
video_in_path = None
image_in_path = None
video_out_path = None
q = queue.Queue()

run_lock = threading.Lock()

def write_noln(text):
    st.configure(state='normal')
    st.insert(END, text)
    st.configure(state='disabled')
    st.yview(END)

def write_ln():
    write_noln('\n')

def write(text):
    write_noln(text)
    write_ln()

def video_in_cb():
    global video_in_path
    x = filedialog.askopenfilename(filetypes=(('Select Video', '*.avi;*.mkv;*.mov;*.mp4;*.mpg'),))
    if x is not None:
        if len(x) > 0:
            video_in_path = x
            write('New video input path: {}'.format(video_in_path))

def image_in_cb():
    global image_in_path
    x = filedialog.askopenfilename(filetypes=(('Select Image', '*.bmp;*.dds;*.dib;*.emf;*.exif;*.gif;*.ico;*.j2c;*.j2k;*.jfif;*.jp2;*.jpc;*.jpe;*.jpeg;*.jpf;*.jpg;*.jps;*.jpx;*.pam;*.pbm;*.pcx;*.pfm;*.pgm;*.png;*.pnm;*.ppm;*.pxr;*.rle;*.tif;*.tiff'),))
    if x is not None:
        if len(x) > 0:
            image_in_path = x
            write('New image input path: {}'.format(image_in_path))

def video_out_cb():
    global video_out_path
    x = filedialog.asksaveasfilename(filetypes=(('.mp4 file', '*.mp4'),))
    if x is not None:
        if len(x) > 0:
            if not x.endswith('.mp4'):
                x = x + '.mp4'
            video_out_path = x
            write('New video output path: {}'.format(video_out_path))

def trace(stage, inputs, aux=None):
    sep = '================================================================================'
    (type_, value, tb) = sys.exc_info()
    q.put(sep)
    q.put('This section contains the details the devs need to fix this issue.\nIf you are reporting a bug, please include this entire section.\nIf you leave out any of it, there is a good chance the devs will not be able to help.')
    q.put('Error: received a {} at stage "{}".'.format(type_.__name__, stage))
    q.put('Message: "{}"'.format(str(value)))
    q.put('Full traceback:')
    for s in traceback.format_tb(tb):
        q.put(s)
    q.put('<log>')
    q.put(aux)
    q.put('</log>')
    q.put('<inputs>')
    q.put(inputs)
    q.put('</inputs>')
    q.put('This is the last line of the crash report section.')
    q.put(sep)

def acceptable_resolution(x, y):
    modulus = 16
    if not (x % modulus == 0):
        x = modulus * (x // modulus + 1)
    if not (y % modulus == 0):
        y = modulus * (y // modulus + 1)
    return x, y

relative = BooleanVar()
relative.set(True)
fix_gamma = BooleanVar()

def group(iterable, amount):
    cache = []
    for i in iterable:
        cache.append(i)
        if len(cache) == amount:
            yield cache
            cache = []
    if len(cache) > 0:
        yield cache

def prepend(iterable, value):
    yield value
    yield from iterable

# this function is from https://github.com/AliaksandrSiarohin/first-order-model/blob/master/demo.py and is slightly modified
def make_animation_batch_(kp_source, driving_video, generator, kp_detector, kp_driving_initial, source, relative=True, adapt_movement_scale=True, cpu=False):
    with torch.no_grad():
        global progress
        predictions = []
        driving = torch.tensor(np.array(driving_video)[np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3)
        for frame_idx in range(driving.shape[2]):
            driving_frame = driving[:, :, frame_idx]
            if not cpu:
                driving_frame = driving_frame.cuda()
            kp_driving = kp_detector(driving_frame)
            kp_norm = normalize_kp(kp_source=kp_source, kp_driving=kp_driving,
                                   kp_driving_initial=kp_driving_initial, use_relative_movement=relative,
                                   use_relative_jacobian=relative, adapt_movement_scale=adapt_movement_scale)
            out = generator(source, kp_source=kp_source, kp_driving=kp_norm)
            predictions.append(np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0])
            del out
            del driving_frame
            progress += 1
    return predictions

def make_animation_batch(source_image, driving_generator, generator, kp_detector, relative=True, adapt_movement_scale=True, cpu=False):
    with torch.no_grad():
        source = torch.tensor(source_image[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2)
        if not cpu:
            source = source.cuda()
        kp_source = kp_detector(source)
        initial = next(driving_generator)
        driving_generator = prepend(driving_generator, initial)
        fake_driving = torch.tensor(np.array(initial)[np.newaxis][np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3)
        kp_driving_initial = kp_detector(fake_driving[:, :, 0])
        for batch in group(driving_generator, 500):
            yield from make_animation_batch_(kp_source, batch, generator, kp_detector, kp_driving_initial, source, relative=relative, adapt_movement_scale=adapt_movement_scale, cpu=cpu)

def resize(img, shape):
    return transform.resize(img, shape, anti_aliasing=True)

def worker_thread(vid0n, img0n, vid1n, cpu, relative, fix_gamma):
    try:
        global progress
        global progress_max
        global stopped
        global checkpoints
        with run_lock:
            if not (cpu == checkpoints['cpu']):
                q.put('Reloading checkpoints...')
                checkpoints['cpu'] = cpu
                reload()
                q.put('Finished reloading checkpoints.')
            if os.path.isfile('tmp.mp4'):
                os.remove('tmp.mp4')
            q.put('Loading sources...')
            vid0r = imageio.get_reader(vid0n)
            fps = vid0r.get_meta_data()['fps']
            vid0 = []
            while True:
                try:
                    im = vid0r.get_next_data()
                except (IndexError, imageio.core.CannotReadFrameError):
                    break
                else:
                    vid0.append(resize(im, (256, 256))[..., :3])
            progress = 0
            progress_max = len(vid0)
            img0 = imageio.imread(img0n)
            size = img0.shape[:2]#[::-1]
            size = acceptable_resolution(size[0], size[1])
            img0 = resize(img0, (256, 256))[..., :3]
            vid1 = imageio.get_writer('tmp.mp4', fps=fps)
            q.put('Sources loaded.\nGenerating frames...')
            for frame in make_animation_batch(img0, iter(vid0), checkpoints['g'], checkpoints['kp'], cpu=cpu, relative=relative):
                if fix_gamma:
                    # thanks @Maca
                    frame = exposure.adjust_gamma(frame, gamma=2.07)
                vid1.append_data(img_as_ubyte(resize(frame, size)))
            vid1.close()
            q.put('Re-encoding video. This may take a while...')
            cmd = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffmpeg.exe')]
            cmd.extend(shlex.split('-y -hide_banner -loglevel warning -i tmp.mp4 -i'))
            cmd.append(vid0n)
            cmd.extend(shlex.split('-map 0:v -map 1:a -movflags faststart -c:v libx264 -pix_fmt yuv420p -preset veryslow -crf 0'))
            if ffmpeg_flags:
                cmd.extend(ffmpeg_flags)
            cmd.append(vid1n)
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            q.put(output)
            #os.remove('tmp.mp4')
    except subprocess.CalledProcessError as e:
        msg = 'command "{}" returned non-zero error code {}: {}'.format(
            e.cmd,
            e.returncode,
            e.output
        )
        trace('ffmpeg', [vid0n, img0n, vid1n], aux=msg)
        q.put('FFmpeg has crashed!\nUsually this means the deepfake process worked, but re-encoding failed.')
        shutil.copy('tmp.mp4', vid1n)
        q.put('You can attempt to salvage your progress by re-muxing audio streams manually.\nThis may also happen if your input video contains no audio; if this is the case,the file should be intact.')
        raise e
    except Exception as e:
        msg = 'cpu={}'.format(cpu)
        trace('predict', [vid0n, img0n, vid1n], aux=msg)
        q.put('Yanderify has crashed!\nSome common problems:\n- You have a non-NVIDIA card. Only NVIDIA cards are supported in GPU mode for\ntechnical reasons. However, you can run in CPU mode, albeit much slower. Please read the disclaimer at the top about CPUs!\n- You have an NVIDIA card, but there is either not enough VRAM or the card is\ntoo old or one of the new RTX 3000 series ones, which have an incompatible\nversion of CUDA. >=700 series cards with >=2GB dedicated VRAM should work fine. Use CPU Mode, if your GPU is unsupported!\n- You have a working card, but there is not enough available VRAM to run the\ndeepfake process. Browsers, video games, video editing softwares commonly cause VRAM issues. If you have any of these open, try closing them.\n- One of your input files is corrupted or unsupported! Check if you can open\nthem in other programs without issues.\n- If you received "MemoryError", then it means that there is not enough RAM\navailable for the deepfake process! Try closing programs, that might be using\ntoo much RAM.\n- One of the devs messed up somewhere. If that is the case, make sure to submit the full crash report (you might have to scroll up!), otherwise we cannot help\nyou!')
        raise e
    except KeyboardInterrupt as e:
        q.put('Stopping...')
    else:
        q.put('Success!\n')
    finally:
        stopped = True

def start():
    global stopped
    if not stopped:
        stopped = True
        return
    write('Starting.')
    if (video_in_path is None) or (image_in_path is None) or (video_out_path is None):
        write('Error: files must be selected!')
        return
    if run_lock.locked():
        write('Error: already started!')
        return
    stopped = False
    threading.Thread(target=worker_thread, args=(video_in_path, image_in_path, video_out_path, use_cpu.get(), relative.get(), fix_gamma.get())).start()

def open_gh():
    webbrowser.open('https://github.com/Animan8000/Yanderify-bit-rate-fix')

adv_panel_shown = False
toggle_adv_panel = False

def adv_toggle_cmd():
    global toggle_adv_panel
    toggle_adv_panel = True

class Yanderify(Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.master = master
        self.grid()
        self.create_widgets()
        self.after(50, self.process_queue)

    def create_widgets(self):
        global st
        master = self.master
        c = Checkbutton(master, text='CPU mode (for non-NVIDIA GPUs)', variable=use_cpu)
        c.grid(row=0, column=0)
        video_in = Button(master, text='Select Video', command=video_in_cb)
        video_in.grid(row=0, column=1)
        image_in = Button(master, text='Select Image', command=image_in_cb)
        image_in.grid(row=0, column=2)
        video_out = Button(master, text='Select Output', command=video_out_cb)
        video_out.grid(row=0, column=3)
        gh_button = Button(master, text='GitHub', command=open_gh)
        gh_button.grid(row=0, column=4)
        self.go = Button(master, text='Start', command=start)
        self.go.grid(row=1, column=4)
        self.progress_bar = Progressbar(master, orient=HORIZONTAL, mode='determinate', length=500)
        self.progress_bar.grid(row=1, column=0, columnspan=4)
        st = scrolledtext.ScrolledText(master, state=DISABLED)
        st.grid(row=2, column=0, columnspan=5, rowspan=7)
        write('Started Yanderify 4.0.3 bit rate fix. (CRF 0)\nDisclaimer: CPU mode on low-end computers or most laptops generally will cause\nthe system to lock-up.\nWe are not liable if you freeze your PC by refusing to listen to this advice.\nIf the gamma looks weird in the output, consider clicking on "Toggle advanced\nsettings" and activate "Fix gamma".\n\nOriginal First Order Motion Model repo: https://github.com/AliaksandrSiarohin/first-order-model\nYanderify repo: https://github.com/dunnousername/yanderifier\nYanderify bit rate fix repo: https://github.com/Animan8000/Yanderify-bit-rate-fix\n')
        adv_toggle = Button(master, text='Toggle advanced settings', command=adv_toggle_cmd)
        adv_toggle.grid(row=9, column=0, columnspan=5)
        self.adv_panel = Frame(master)
        adv_relative = Checkbutton(self.adv_panel, text='Relative', variable=relative)
        adv_relative.grid(row=0, column=0)
        adv_gamma = Checkbutton(self.adv_panel, text='Fix gamma', variable=fix_gamma)
        adv_gamma.grid(row=1, column=0)

    def process_queue(self):
        global toggle_adv_panel
        global adv_panel_shown
        if toggle_adv_panel:
            toggle_adv_panel = False
            adv_panel_shown = not adv_panel_shown
            if adv_panel_shown:
                self.adv_panel.grid(row=10, column=0, rowspan=3, columnspan=5)
            else:
                self.adv_panel.grid_remove()
        self.progress_bar['value'] = 100 * min(1.0, progress / max(progress_max, 1.0))
        self.go['text'] = 'Start' if stopped else 'Stop'
        try:
            while True:
                msg = q.get(block=False)
                write(msg)
        except queue.Empty:
            self.after(50, self.process_queue)

app = Yanderify(master=root)
app.mainloop()
