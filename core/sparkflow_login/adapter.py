"""8000 wiring for the unchanged 8003 LoginDesktopManager implementation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit

from core.qr_login import LoginObservation
from core.qr_profile import prepare_profile, clear_profile_login_data
from . import desktop


class _AsyncWorker:
    """Keep the source's async browser loop running between HTTP polls."""
    def __init__(self):
        self._ready=threading.Event()
        self._thread=threading.Thread(target=self._work,name='sparkflow-login',daemon=True)
        self._thread.start()
        if not self._ready.wait(5):raise RuntimeError('login_loop_not_ready')
    def _work(self):
        self._loop=asyncio.ProactorEventLoop() if sys.platform=='win32' else asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop);self._ready.set()
        try:self._loop.run_forever()
        finally:
            pending=asyncio.all_tasks(self._loop)
            for task in pending:task.cancel()
            if pending:self._loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
    def run(self,coroutine,timeout=45):
        future=asyncio.run_coroutine_threadsafe(coroutine,self._loop)
        try:return future.result(timeout)
        except BaseException:future.cancel();raise
    def close(self):
        if not self._thread.is_alive():return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if self._thread.is_alive():raise RuntimeError('login_loop_close_failed')


class SparkFlowLoginBrowser:
    def __init__(self,*,profile_data_dir,channel=None,manager_factory=None,runner_factory=None):
        if profile_data_dir is None:raise ValueError('login_profile_not_configured')
        self._profile_dir=prepare_profile(profile_data_dir)
        desktop.PROFILE_DIR=self._profile_dir
        desktop.LOGIN_NETWORK_MODE=os.getenv('QR_LOGIN_NETWORK_MODE','direct').strip().lower() or 'direct'
        if desktop.LOGIN_NETWORK_MODE not in {'auto','direct','proxy'}:
            raise ValueError('unsupported_login_network_mode')
        os.environ['SPARKFLOW_BROWSER_CHANNEL']=channel or os.getenv('QR_LOGIN_BROWSER_CHANNEL','msedge') or 'msedge'
        self._runner=(runner_factory or _AsyncWorker)()
        self._closed=False;self._closing=False;self._context_ref=None;self._context_closed=True
        self._playwright_ref=None;self._driver_stopped=False
        self._cleanup_manager_done=False;self._profile_cleaned=False
        self._verification_key=None;self._verification_generation=0
        self._original_purge=desktop._purge_login_data
        def purge_owned_profile(path):
            if Path(path).resolve()!=self._profile_dir.resolve():raise RuntimeError('unexpected_login_profile')
            if self._closing and self._context_ref is not None and not self._context_closed:
                raise RuntimeError('login_context_not_closed')
            if self._closing and self._profile_cleaned:return
            clear_profile_login_data(self._profile_dir)
            if self._closing:self._profile_cleaned=True
        self._purge_guard=purge_owned_profile
        desktop._purge_login_data=self._purge_guard
        try:self._manager=self._runner.run(self._create_manager(manager_factory or desktop.LoginDesktopManager))
        except BaseException:
            desktop._purge_login_data=self._original_purge
            self._runner.close();raise

    async def _create_manager(self,factory):
        manager=factory()
        original_stop=manager.stop
        async def stop(*,clear_profile=False):
            if clear_profile:raise RuntimeError('whole_login_profile_removal_disabled')
            if self._cleanup_manager_done:return
            self._bind_context(manager.context)
            if manager.playwright is not None:
                if manager.playwright is not self._playwright_ref:
                    self._playwright_ref=manager.playwright;self._driver_stopped=False
                # The vendor stop swallows driver failures and clears its
                # reference. Keep it owned here and await it without suppression.
                manager.playwright=None
            if manager.context is None and self._context_ref is not None and not self._context_closed:
                manager.context=self._context_ref
            self._closing=True
            try:
                await original_stop(clear_profile=False)
                if self._context_ref is not None and not self._context_closed:
                    raise RuntimeError('login_context_not_closed')
                if self._playwright_ref is not None and not self._driver_stopped:
                    await self._playwright_ref.stop()
                    self._driver_stopped=True
                self._cleanup_manager_done=True
            finally:self._closing=False
        manager.stop=stop
        return manager

    def _bind_context(self,context):
        if context is None or context is self._context_ref:return
        self._context_ref=context;self._context_closed=False
        self._cleanup_manager_done=False;self._profile_cleaned=False
        context.on('close',lambda *_:setattr(self,'_context_closed',True))

    async def _open(self):
        await self._manager.open_login()
        self._bind_context(self._manager.context)
    def open(self):self._runner.run(self._open())
    def refresh(self):self._runner.run(self._manager.refresh_login_qr())

    async def _verification_target(self):
        page=self._manager.page
        for frame in page.frames:
            address=urlsplit(frame.url)
            if address.scheme!='https' or address.hostname!='rmc.bytedance.com' or not address.path.startswith('/verifycenter/captcha/'):
                continue
            try:
                element=await frame.frame_element()
                if not await element.is_visible():continue
                box=await element.bounding_box();viewport=page.viewport_size or {'width':1600,'height':1000}
                if not box:continue
                x,y=max(0,box['x']),max(0,box['y'])
                width=min(box['x']+box['width'],viewport['width'])-x
                height=min(box['y']+box['height'],viewport['height'])-y
                if width<32 or height<32:continue
                clip={'x':x,'y':y,'width':width,'height':height}
                return clip,(frame.url,tuple(round(clip[name],1) for name in ('x','y','width','height')))
            except Exception:continue
        address=urlsplit(page.url)
        if address.scheme=='https' and address.hostname in {'www.douyin.com','creator.douyin.com','rmc.bytedance.com'} and (await page.title()).strip()=='验证码中间页':
            viewport=page.viewport_size or {'width':1600,'height':1000}
            clip={'x':0,'y':0,'width':viewport['width'],'height':viewport['height']}
            return clip,('official-verification-interstitial',page.url,tuple(clip.values()))
        return None

    async def _observe(self):
        page=self._manager.page
        if page is None or page.is_closed():raise RuntimeError('browser_closed')
        target=await self._verification_target()
        if target is not None:
            clip,key=target
            if key!=self._verification_key:self._verification_generation+=1
            self._verification_key=key
            png=await page.screenshot(type='png',clip=clip,timeout=5000)
            return LoginObservation('verification_required',verification_png=png,
                verification_width=round(clip['width']),verification_height=round(clip['height']),
                verification_generation=self._verification_generation)
        self._verification_key=None
        result=await self._manager.export_if_ready()
        if result.get('state')=='ready':
            return LoginObservation('saving',authenticated=True,login_signal='creator_identity')
        qr=await self._manager._extract_login_qr(page)
        if qr is not None:
            await self._manager.reduce_page_activity(page)
            png=await qr.screenshot(type='png',timeout=5000)
            return LoginObservation('waiting',qr_png=png)
        return LoginObservation('qr_not_ready')
    def observe(self):return self._runner.run(self._observe(),timeout=15)

    async def _state(self):return await self._manager.context.storage_state()
    def storage_state(self):return self._runner.run(self._state(),timeout=10)

    async def _interact(self,action,points,generation,still_current):
        target=await self._verification_target()
        if target is None or generation!=self._verification_generation or target[1]!=self._verification_key or not still_current():return False
        clip,key=target;mouse=self._manager.page.mouse;pressed=False
        def position(point):return clip['x']+point['x']*(clip['width']-1),clip['y']+point['y']*(clip['height']-1)
        try:
            await mouse.move(*position(points[0]))
            fresh=await self._verification_target()
            if not still_current() or fresh is None or fresh[1]!=key:return False
            await mouse.down();pressed=True
            if action=='drag':
                started=time.monotonic()
                for point in points[1:]:
                    until=started+(point['t']-points[0]['t'])/1000
                    while time.monotonic()<until:
                        if not still_current():return False
                        await asyncio.sleep(min(.04,max(0,until-time.monotonic())))
                    fresh=await self._verification_target()
                    if not still_current() or fresh is None or fresh[1]!=key:return False
                    await mouse.move(*position(point))
            return True
        finally:
            try:
                if pressed:await mouse.up()
            finally:self._verification_key=None
    def interact(self,action,points,generation,still_current):
        return self._runner.run(self._interact(action,points,generation,still_current),timeout=10)

    def close(self):
        if self._closed:return
        try:
            if not self._cleanup_manager_done:
                self._runner.run(self._manager.stop(clear_profile=False),timeout=15)
            if self._context_ref is not None and not self._context_closed:
                raise RuntimeError('login_context_not_closed')
            self._runner.close()
        except BaseException as exc:
            # Preserve the owned context/driver, runner and profile guard for
            # another close attempt on the original worker thread.
            raise RuntimeError('cleanup_failed') from exc
        self._closed=True
        if desktop._purge_login_data is self._purge_guard:desktop._purge_login_data=self._original_purge
