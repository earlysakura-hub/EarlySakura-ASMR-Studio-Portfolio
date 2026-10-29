"""Persistent player controls. Audio/subtitles remain in the existing worker."""
import tkinter as tk
from tkinter import ttk
import time


class PlayerPage(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent,padding=16); self.pack(fill='both',expand=True)
        self.app=app; self.dragging=False; self.volume_dragging=False; self.pending_position=None
        self.duration=0; self.position=0; self.ready=False
        self.last_info={}
        ttk.Label(self,text='播放器').pack(anchor='w')
        ttk.Label(self,textvariable=app.player_status,wraplength=900).pack(anchor='w',pady=12)
        ttk.Label(self,text='播放与悬浮字幕由原播放器负责；切换标签不会停止播放。',wraplength=900).pack(anchor='w')
        bar=ttk.Frame(self); bar.pack(fill='x',pady=14)
        for key,label,callback in (('select','选择音频',app.select_audio),('player','打开所选作品',lambda:app.launch('player')),
                                   ('stop_player','关闭播放器',app.stop_player)):
            button=ttk.Button(bar,text=label,command=callback); button.pack(side='left',padx=(0,8))
            app.player_buttons[key]=button
        controls=ttk.Frame(self); controls.pack(fill='x',pady=(0,12)); self.controls=[]
        for label,command,fields in (('暂停 / 继续','toggle',{}),('后退 5 秒','relative',{'seconds':-5}),
                                     ('前进 5 秒','relative',{'seconds':5}),('显示 / 隐藏悬浮字幕','visibility',{})):
            button=ttk.Button(controls,text=label,command=lambda c=command,f=fields:app.player_command(c,**f))
            button.pack(side='left',padx=(0,8)); self.controls.append(button)
        self.clock=tk.StringVar(value='00:00:00 / 00:00:00'); ttk.Label(self,textvariable=self.clock).pack(anchor='w')
        self.progress=ttk.Scale(self,from_=0,to=100); self.progress.pack(fill='x',pady=8)
        self.progress.bind('<ButtonPress-1>',self.begin_drag)
        self.progress.bind('<B1-Motion>',self.drag)
        self.progress.bind('<ButtonRelease-1>',self.release)
        self.progress.bind('<KeyRelease>',self.keyboard_seek)
        row=ttk.Frame(self); row.pack(fill='x',pady=12)
        ttk.Label(row,text='音量').pack(side='left',padx=(0,8))
        self.volume=ttk.Scale(row,from_=0,to=100); self.volume.set(100); self.volume.pack(side='left',fill='x',expand=True)
        self.volume.bind('<ButtonRelease-1>',self.set_volume); self.volume.bind('<KeyRelease>',self.set_volume)
        self.volume.bind('<ButtonPress-1>',lambda event:setattr(self,'volume_dragging',True))
        self.update_controls()

    def update_controls(self):
        job=getattr(self.app,'player_job',None)
        enabled=bool(job and not job.stop_requested and self.ready and not self.app.exiting)
        for button in self.controls: button.configure(state='normal' if enabled else 'disabled')
        for scale in (self.progress,self.volume): scale.configure(state='normal' if enabled else 'disabled')

    def observe(self, info):
        position,duration=info.get('position_ms',0),info.get('duration_ms',0)
        if type(position) is not int or type(duration) is not int or min(position,duration)<0: return
        self.last_info=dict(info)
        self.position,self.duration=position,duration
        self.ready=bool(info.get('controls_ready',info.get('state') in ('Playing','Paused','Ended')))
        def format_time(ms):
            seconds=ms//1000; return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'
        self.clock.set(f'{format_time(position)} / {format_time(duration)}')
        if self.pending_position and (abs(position-self.pending_position[0])<=750 or time.monotonic()>self.pending_position[1]):
            self.pending_position=None
        if not self.dragging and self.pending_position is None:
            self.progress.set(min(100,position/duration*100) if duration else 0)
        volume=info.get('volume')
        if type(volume) is int and 0<=volume<=100 and not self.volume_dragging: self.volume.set(volume)
        self.update_controls()

    def begin_drag(self,event):
        if not self.ready: return 'break'
        self.dragging=True; self.drag(event); return 'break'

    def drag(self,event):
        if self.dragging: self.progress.set(max(0,min(100,event.x*100/max(1,self.progress.winfo_width()))))
        return 'break'

    def release(self,event):
        if not self.dragging: return 'break'
        self.drag(event); self.dragging=False; self.seek(); return 'break'

    def keyboard_seek(self,event=None): self.seek()

    def seek(self):
        if self.ready and self.duration>0:
            position=int(self.duration*self.progress.get()/100)
            self.pending_position=(position,time.monotonic()+5)
            self.app.player_command('seek',position_ms=position)

    def set_volume(self,event=None):
        self.volume_dragging=False
        if self.ready: self.app.player_command('volume',value=round(self.volume.get()))

    def reset(self,clear=False):
        self.ready=False; self.dragging=False; self.volume_dragging=False; self.pending_position=None
        if clear:
            self.last_info={}
            self.clock.set('00:00:00 / 00:00:00'); self.progress.set(0); self.volume.set(100)
        self.update_controls()
