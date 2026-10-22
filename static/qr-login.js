/* Same-origin QR login. Job capabilities stay in memory and only in headers. */
(function () {
  'use strict';
  const BASE = '/api/credentials/qr';
  const LABELS = {
    idle: '点击开始扫码登录，在本页完成凭证更新。',
    starting: '正在准备登录二维码…', waiting: '请打开抖音 App 扫描下方二维码。',
    qr_ready: '请打开抖音 App 扫描下方二维码。', scanned: '已扫描二维码，请在手机上确认登录。',
    confirming: '正在等待手机确认登录…', saving: '登录已确认，正在自动保存凭证…',
    success: '登录成功，state.json 已自动更新，无需手动上传。',
    expired: '二维码已过期，请点击刷新二维码后重新扫码。',
    verification_required: '请在图中点击或拖动完成官方验证，通过后会自动显示二维码。',
    blocked: '平台暂时拦截了本次登录，本页不处理额外验证。请稍后再试或手动上传登录态。',
    failed: '本次扫码登录未完成，原有登录态保留。',
    cancelled: '本次扫码登录已取消，原有登录态保留。',
    timeout: '本次登录等待超时，请重新开始。', cancelling: '正在取消本次扫码登录…',
    sync_pending: '操作结果暂未确认，正在同步登录状态；请勿重复开始。',
    cleanup_pending: '正在确认旧登录浏览器已关闭；完成清理前不能开始新的登录。'
  };
  const STOP = new Set(['success', 'failed', 'cancelled', 'timeout', 'expired', 'blocked']);
  const TERMINAL = new Set(['success', 'failed', 'cancelled', 'timeout']);
  function deadlineMillis(value) {
    if (typeof value === 'number') return value < 100000000000 ? value * 1000 : value;
    if (typeof value === 'string' && /^\d+(\.\d+)?$/.test(value)) return deadlineMillis(Number(value));
    const parsed = Date.parse(value || ''); return Number.isFinite(parsed) ? parsed : 0;
  }
  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  function create(options) {
    if (!options || !options.api) throw new Error('QR login requires a same-origin API client');
    const api = options.api;
    let disposed = false, generation = 0, pollTimer = null, job = null, pendingAction = null;
    let pendingStartRequest = null;
    let lastImageAt = 0, lastRevision = null, imagePending = false, notifiedJob = null, qrRequest = null;
    let verificationRequest = null, verificationFrame = null, interaction = null, lastVerificationAt = 0;
    let statusSerial = 0, minimumStatusSerial = 0, interactionError = '';
    let statusError = '', imageError = '', verificationImageError = '';
    let serverCleanupRetrying = false;
    let verification = {ready:false,revision:null,width:0,height:0,busy:false};
    const reads = new Set();
    let state = {status:'idle', running:false, message:LABELS.idle, error:'', imageUrl:'', deadline:0, hasJob:false, canRefresh:false,
      verificationImageUrl:'',verificationReady:false,verificationRevision:null,verificationWidth:0,verificationHeight:0,
      verificationBusy:false,interactionPending:false,gestureActive:false,
      statusError:'',imageError:'',verificationImageError:'',canRetrySync:false,canRetryImage:false,
      saved:false,cleanupComplete:true,cleanupRetrying:false,canRetryCleanup:false,canRetryStart:false,startUnconfirmed:false};
    const snapshot = () => ({...state});
    function publish(patch) {
      state = {...state, ...patch};
      state.statusError=statusError;state.imageError=imageError;state.verificationImageError=verificationImageError;
      state.error=[statusError,imageError,verificationImageError,interactionError].filter(Boolean).join(' ');
      state.cleanupRetrying=pendingAction?.kind==='cleanup'||serverCleanupRetrying;
      state.startUnconfirmed=!!pendingStartRequest&&!job;
      state.canRetryStart=state.startUnconfirmed&&!pendingAction&&!disposed;
      state.canRetrySync=!!job&&!pendingAction&&!STOP.has(state.status)&&state.status!=='cancelling'&&
        (!!statusError||['sync_pending','cleanup_pending'].includes(state.status));
      state.canRetryCleanup=!!job&&!pendingAction&&!state.cleanupRetrying&&state.status==='cleanup_pending';
      state.canRetryImage=!!job&&!pendingAction&&!STOP.has(state.status)&&!['sync_pending','cleanup_pending','cancelling'].includes(state.status)&&
        (state.status==='verification_required'
          ? !!verificationImageError&&verification.ready&&!interaction&&!verification.busy&&!verificationRequest
          : !!imageError&&!imagePending);
      if (!disposed && typeof options.onChange === 'function') options.onChange(snapshot());
    }
    function current(attempt) { return !disposed && attempt === generation; }
    function stopReads() {
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = null;
      for (const controller of reads) controller.abort();
      reads.clear(); imagePending = false; qrRequest = null; verificationRequest = null;
    }
    function clearImage() {
      if (qrRequest) qrRequest.controller.abort();
      qrRequest=null;imagePending=false;lastImageAt=0;lastRevision=null;
      imageError='';
      if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
      publish({imageUrl:''});
    }
    function clearVerificationImage() {
      if (verificationRequest) verificationRequest.controller.abort();
      verificationRequest=null;verificationFrame=null;lastVerificationAt=0;
      if (state.verificationImageUrl) URL.revokeObjectURL(state.verificationImageUrl);
      publish({verificationImageUrl:'',verificationRevision:null,verificationWidth:0,verificationHeight:0});
    }
    function releaseInteraction() {
      const previous=interaction;interaction=null;
      try { if(previous?.element?.hasPointerCapture?.(previous.pointerId))previous.element.releasePointerCapture(previous.pointerId); } catch (_) {}
      publish({interactionPending:verification.busy,gestureActive:false});
    }
    function clearVerification() {
      verification={ready:false,revision:null,width:0,height:0,busy:false};interactionError='';verificationImageError='';
      releaseInteraction();clearVerificationImage();
      publish({verificationReady:false,verificationBusy:false,interactionPending:false});
    }
    function headers(active, mutation) {
      const value = {};
      if (active) value['X-QR-Job-Token'] = active.token;
      if (mutation) { value['X-Spark-QR-Request'] = '1'; value['X-Requested-With'] = 'sparkkeeper'; }
      return value;
    }
    function handle(data, fallback) {
      const id = typeof data?.job_id === 'string' ? data.job_id : fallback?.id;
      const token = typeof data?.job_token === 'string' ? data.job_token : (id === fallback?.id ? fallback?.token : '');
      if (!id || !token) throw new Error('登录任务响应不完整，请重新开始。');
      return {id, token};
    }
    function messageFrom(error, fallback) {
      const detail = error?.response?.data?.detail;
      return typeof detail === 'string' ? detail.slice(0, 240) : fallback;
    }
    function cancelRequest(active, keepalive) {
      if (!active) return Promise.resolve(null);
      // Keep the same API client for navigation/unmount cancellation;
      // only the QR job capability and same-origin marker authorize the job.
      const config = {headers:headers(active,true), timeout:15000};
      if (keepalive) { config.adapter='fetch'; config.fetchOptions={keepalive:true}; }
      return api.post(BASE + '/cancel', {job_id:active.id}, config).then(response => response.data);
    }
    function apply(data, attempt, serial=null) {
      if (!current(attempt) || serial!==null && serial<minimumStatusSerial) return;
      const aliases = {qr_expired:'expired',error:'failed',verifying:'confirming',verification:'verification_required'};
      const raw = String(data?.status || 'waiting');
      let status = aliases[raw] || (Object.prototype.hasOwnProperty.call(LABELS, raw) ? raw : 'waiting');
      const saved=typeof data?.saved==='boolean'?data.saved:(status==='success'||state.saved);
      const cleanupComplete=typeof data?.cleanup_complete==='boolean'?data.cleanup_complete:
        (state.status==='cleanup_pending'?false:status!=='cleanup_pending');
      if(!cleanupComplete&&(STOP.has(status)||saved))status='cleanup_pending';
      if(typeof data?.cleanup_retrying==='boolean')serverCleanupRetrying=data.cleanup_retrying;
      if(status!=='cleanup_pending')serverCleanupRetrying=false;
      if(serial!==null)minimumStatusSerial=Math.max(minimumStatusSerial,serial);
      statusError='';
      const serverMessage = typeof data?.message === 'string' ? data.message.slice(0, 300) : '';
      if (status==='verification_required') {
        if(state.status!=='verification_required') { clearImage();clearVerification(); }
        if(typeof data?.verification_error==='string' && data.verification_error)interactionError=data.verification_error.slice(0,240);
        const revision=Number.isSafeInteger(data?.verification_revision) && data.verification_revision>=0 ? data.verification_revision : null;
        const width=Number.isFinite(data?.verification_width) && data.verification_width>0 ? data.verification_width : 0;
        const height=Number.isFinite(data?.verification_height) && data.verification_height>0 ? data.verification_height : 0;
        if(verification.revision===null || revision!==null && revision>=verification.revision) {
          verification={ready:data?.verification_ready===true,revision,width,height,busy:data?.verification_busy===true};
        }
        if(interaction && interaction.phase!=='tracking' && data?.verification_busy===false &&
          (revision!==null && revision>interaction.revision || interaction.phase==='uncertain' && serial!==null && serial>=minimumStatusSerial)) {
          releaseInteraction();clearVerificationImage();
        }
        if(!interaction && verificationFrame && (verificationFrame.revision!==verification.revision ||
          verificationFrame.width!==verification.width || verificationFrame.height!==verification.height || !verification.ready))clearVerificationImage();
      } else if (state.status==='verification_required' || state.verificationImageUrl || interaction) clearVerification();
      if(status==='cleanup_pending'){clearImage();clearVerification();}
      publish({status, running:!STOP.has(status), message:status==='verification_required'?LABELS[status]:(serverMessage || LABELS[status]),
        deadline:deadlineMillis(data?.deadline) || state.deadline,
        saved,cleanupComplete,
        verificationReady:verification.ready,verificationBusy:verification.busy,
        interactionPending:!!interaction || verification.busy,gestureActive:interaction?.phase==='tracking',
        hasJob:!!job, canRefresh:!!job && ['starting','waiting','qr_ready','scanned','confirming','expired'].includes(status)});
      if (STOP.has(status)) {
        stopReads(); clearImage();clearVerification();
        if (status === 'success') {
          const id = job?.id;
          job = null; publish({hasJob:false,canRefresh:false,message:LABELS.success});
          if (id && notifiedJob !== id) {
            notifiedJob = id;
            Promise.resolve().then(() => { if (current(attempt)) return options.onSuccess?.(); }).catch(() => {
              if (current(attempt)) {statusError='凭证已保存，但页面状态刷新失败，请稍后刷新页面。';publish({});}
            });
          }
        } else if (TERMINAL.has(status)) { job=null; publish({hasJob:false,canRefresh:false}); }
      }
    }
    async function fetchImage(attempt, force=false, revision=null) {
      if (!current(attempt) || !job || imagePending || STOP.has(state.status) || ['verification_required','cleanup_pending','sync_pending'].includes(state.status)) return false;
      if (!force && revision === lastRevision && Date.now() - lastImageAt < 10000) return false;
      const active=job, controller=new AbortController(), request={controller}; reads.add(controller); imagePending=true;qrRequest=request;
      lastImageAt=Date.now();lastRevision=revision;
      try {
        const response=await api.get(BASE+'/image', {params:{job_id:active.id},headers:headers(active,false),responseType:'blob',signal:controller.signal,timeout:10000});
        if (!current(attempt) || job?.id !== active.id || STOP.has(state.status) || state.status==='verification_required' || qrRequest!==request) return false;
        lastImageAt=Date.now();
        if (response.status === 202 || response.status === 204) return false;
        const blob=response.data;
        if (!blob || !/^image\/(png|jpeg|webp)$/.test(blob.type || '') || !blob.size) {
          imageError='二维码图片响应无效，请重新加载图片。';publish({});return false;
        }
        const next=URL.createObjectURL(blob), previous=state.imageUrl;
        lastRevision=revision;
        imageError='';publish({imageUrl:next});
        if (previous) URL.revokeObjectURL(previous);
        return true;
      } catch (error) {
        if (current(attempt) && !controller.signal.aborted && error?.response?.status !== 202 && error?.response?.status !== 204)
          {imageError='二维码暂未加载，正在重试；也可以点击重新加载图片。';publish({});}
        return false;
      } finally { reads.delete(controller); if(qrRequest===request){qrRequest=null;imagePending=false;if(current(attempt))publish({});} }
    }
    async function fetchVerification(attempt, force=false) {
      if(!current(attempt) || !job || state.status!=='verification_required' || interaction || verification.busy || verificationRequest ||
        !verification.ready || verification.revision===null || !verification.width || !verification.height)return false;
      if(!force && Date.now()-lastVerificationAt<1500)return false;
      const active=job, frame={...verification},controller=new AbortController(),request={controller};
      verificationRequest=request;reads.add(controller);lastVerificationAt=Date.now();
      try {
        const response=await api.get(BASE+'/verification-image',{params:{job_id:active.id},headers:headers(active,false),responseType:'blob',signal:controller.signal,timeout:10000});
        if(!current(attempt) || job?.id!==active.id || state.status!=='verification_required' || interaction || verification.busy ||
          verificationRequest!==request || frame.revision!==verification.revision || frame.width!==verification.width || frame.height!==verification.height)return false;
        if(response.status===202 || response.status===204)return false;
        const blob=response.data;
        if(!blob || blob.type!=='image/png' || !blob.size){verificationImageError='官方验证图片响应无效，请重新加载图片。';publish({});return false;}
        const next=URL.createObjectURL(blob),previous=state.verificationImageUrl;
        verificationFrame={...frame,url:next};
        verificationImageError='';publish({verificationImageUrl:next,verificationRevision:frame.revision,verificationWidth:frame.width,verificationHeight:frame.height});
        if(previous)URL.revokeObjectURL(previous);
        return true;
      } catch(error) {
        if(current(attempt) && verificationRequest===request && !controller.signal.aborted && !interaction)
          {verificationImageError='官方验证图片暂未加载，正在重试；也可以点击重新加载图片。';publish({});}
        return false;
      } finally {reads.delete(controller);if(verificationRequest===request){verificationRequest=null;if(current(attempt))publish({});}}
    }
    function refreshVisibleImage(attempt, force=false, revision=null) {
      if(['cleanup_pending','sync_pending'].includes(state.status))return;
      if(state.status==='verification_required')void fetchVerification(attempt,force);
      else void fetchImage(attempt,force,revision);
    }
    function schedule(attempt) {
      if (!current(attempt) || !job || STOP.has(state.status) || pendingAction?.kind==='cleanup') return;
      if(pollTimer!==null)clearTimeout(pollTimer);
      pollTimer=setTimeout(() => { pollTimer=null; void poll(attempt); }, 1500);
    }
    async function poll(attempt) {
      if (!current(attempt) || !job || STOP.has(state.status)) return false;
      const active=job, controller=new AbortController(),serial=++statusSerial; reads.add(controller);
      try {
        const response=await api.get(BASE+'/status', {params:{job_id:active.id},headers:headers(active,false),signal:controller.signal,timeout:10000});
        if (!current(attempt) || job?.id !== active.id || serial<minimumStatusSerial) return false;
        apply(response.data,attempt,serial);
        if (!STOP.has(state.status) && serial>=minimumStatusSerial)refreshVisibleImage(attempt,false,response.data?.qr_revision ?? null);
        return true;
      } catch (error) {
        if (current(attempt) && !controller.signal.aborted && serial>=minimumStatusSerial) {
          if ([404,410].includes(error?.response?.status)&&state.status!=='cleanup_pending') apply({status:'timeout'},attempt);
          else {statusError=messageFrom(error,'状态连接暂时中断，正在重试，请勿反复开始登录。');publish({});}
        }
        return false;
      } finally { reads.delete(controller); schedule(attempt); }
    }
    async function setup(kind, recoverStart=false) {
      if (disposed || pendingAction || state.status==='cancelling' || kind==='start' && (state.running || job)) return false;
      if (kind==='refresh' && (!job || !state.canRefresh)) return false;
      if (kind==='start' && pendingStartRequest && !recoverStart) return false;
      if (recoverStart && (!pendingStartRequest || job)) return false;
      const attempt=++generation, previous=job;
      statusError='';serverCleanupRetrying=false;
      stopReads(); clearImage();clearVerification(); lastImageAt=0;lastRevision=null;
      publish({status:'starting',running:true,message:kind==='refresh'?'正在刷新二维码…':LABELS.starting,canRefresh:false,deadline:0,saved:false,cleanupComplete:true});
      if(kind==='start'&&!pendingStartRequest)pendingStartRequest=uuid();
      const data=kind==='start'?{request_id:pendingStartRequest}:{job_id:previous.id};
      const promise=api.post(BASE+'/'+kind,data,{headers:headers(kind==='start'?null:previous,true),timeout:15000})
        .then(response=>response.data).catch(async originalError=>{
          // A reload loses the in-memory capability. Recover only an already
          // blocked cleanup task; never restart it or replay a cleanup action.
          if(kind!=='start'||originalError?.response?.status!==409||!current(attempt))throw originalError;
          publish({message:'正在检查上次登录浏览器的清理状态…'});
          try{
            const response=await api.post(BASE+'/recover-cleanup',{}, {headers:headers(null,true),timeout:15000});
            const recovered=response.data;
            if(recovered?.status!=='cleanup_pending'||recovered.cleanup_complete!==false)throw originalError;
            return recovered;
          }catch(_){throw originalError;}
        });
      const action={promise,previous,attempt};pendingAction=action;
      try {
        const result=await promise;
        const active=handle(result,previous);
        if (!current(attempt)) return false; // cancellation/disposal owns the late handle
        if(kind==='start')pendingStartRequest=null;
        job=active;apply(result,attempt);
        if (!STOP.has(state.status)) { refreshVisibleImage(attempt,true,result?.qr_revision ?? null);schedule(attempt); }
        return true;
      } catch (error) {
        if (current(attempt)) {
          job=previous;
          if(job){
            statusError=messageFrom(error,'刷新结果暂未确认，正在同步服务端状态。');
            publish({status:'sync_pending',running:true,hasJob:true,canRefresh:false,message:LABELS.sync_pending});schedule(attempt);
          }else {
            const status=error?.response?.status;
            const uncertain=kind==='start'&&(!status||status>=500);
            if(uncertain){
              statusError='开始请求的结果尚未确认。请点击“恢复本次扫码”，沿用原请求找回任务；不会自动重新开始或取消。';
              publish({status:'sync_pending',running:false,hasJob:false,canRefresh:false,message:statusError});
            }else{
              if(kind==='start')pendingStartRequest=null;
              publish({status:'failed',running:false,hasJob:false,canRefresh:false,message:messageFrom(error,'无法开始二维码登录，请稍后重试。')});
            }
          }
        }
        return false;
      } finally { if(pendingAction===action){pendingAction=null;if(current(attempt))publish({});} }
    }
    async function cancel() {
      if (disposed || pendingAction?.kind==='cleanup' || state.status==='cancelling' || !job && !pendingAction) return false;
      const attempt=++generation, action=pendingAction, previous=job;
      statusError='';stopReads();clearImage();clearVerification();publish({status:'cancelling',running:true,canRefresh:false,message:LABELS.cancelling});
      let active=previous;
      try {
        if(action) { try { active=handle(await action.promise,action.previous); } catch (_) {} }
        if(current(attempt))job=active;
        const data=await cancelRequest(active,false);
        if(!current(attempt))return false;
        job=active;
        apply(data?.status ? data : {status:'cancelled'},attempt);
        if(!STOP.has(state.status))schedule(attempt);
        return true;
      } catch(error) {
        if(current(attempt)){
          statusError='取消请求暂未确认，正在同步状态；可以重试同步或稍后再次取消。';
          publish({status:job?'sync_pending':'failed',running:!!job,message:job?LABELS.sync_pending:statusError,hasJob:!!job,canRefresh:false});
          if(job)schedule(attempt);
        }
        return false;
      }
    }
    async function retryStart() {
      if(disposed||!pendingStartRequest||job||pendingAction)return false;
      return setup('start',true);
    }
    async function retrySync() {
      if(disposed||!job||pendingAction||state.status==='cancelling'||STOP.has(state.status))return false;
      if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;
      minimumStatusSerial=statusSerial+1;
      return poll(generation);
    }
    async function retryImage() {
      if(disposed||!job||pendingAction||interaction||verification.busy||STOP.has(state.status)||['sync_pending','cleanup_pending','cancelling'].includes(state.status))return false;
      return state.status==='verification_required'?fetchVerification(generation,true):fetchImage(generation,true,lastRevision);
    }
    async function retryCleanup() {
      if(disposed||!job||pendingAction||state.cleanupRetrying||state.status!=='cleanup_pending')return false;
      const attempt=generation,active=job;
      minimumStatusSerial=statusSerial+1;
      stopReads();statusError='';
      const promise=api.post(BASE+'/retry-cleanup',{job_id:active.id},{headers:headers(active,true),timeout:15000}).then(response=>response.data);
      const action={promise,previous:active,attempt,kind:'cleanup'};pendingAction=action;publish({});
      try{
        const data=await promise;
        if(!current(attempt)||job?.id!==active.id)return false;
        apply(data,attempt);return true;
      }catch(error){
        if(current(attempt)&&job?.id===active.id){statusError=messageFrom(error,'清理结果暂未确认，请同步状态后再决定是否重试清理。');publish({});}
        return false;
      }finally{
        if(pendingAction===action){pendingAction=null;if(current(attempt)){publish({});schedule(attempt);}}
      }
    }
    function dispose() {
      if(disposed)return;
      const previous=job, action=pendingAction;
      disposed=true;++generation;stopReads();clearImage();clearVerification();job=null;
      window.removeEventListener('pagehide',dispose);
      // Let an in-flight setup return its capability so it can be cancelled.
      // The server deadline also bounds a job if the page disappears first.
      const active=action?action.promise.then(data=>handle(data,action.previous)).catch(()=>previous):Promise.resolve(previous);
      active.then(value=>cancelRequest(value,true)).catch(()=>{});
    }
    function pointerTime(event) {return Number.isFinite(event.timeStamp)?event.timeStamp:Date.now();}
    function sample(event, active) {
      const currentRect=active.element.getBoundingClientRect(),rect=active.rect;
      if(['left','top','width','height'].some(key=>Math.abs(currentRect[key]-rect[key])>1))return null;
      const x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height,t=Math.round(pointerTime(event)-active.startedAt);
      if(![x,y,t].every(Number.isFinite) || x<0 || x>1 || y<0 || y>1 || t<0 || t>3000)return null;
      return {x,y,t};
    }
    function appendPoint(active, point) {
      const previous=active.points[active.points.length-1];
      if(point.t<previous.t)return false;
      active.points.push(point);
      if(active.points.length>256)active.points=active.points.filter((_,index,array)=>index===0 || index===array.length-1 || index%2===0);
      active.travel=Math.max(active.travel,Math.hypot((point.x-active.points[0].x)*active.rect.width,(point.y-active.points[0].y)*active.rect.height));
      return true;
    }
    function abortGesture(message='本次操作已取消，请在验证图中重新点击或拖动。') {
      if(!interaction || interaction.phase!=='tracking')return false;
      releaseInteraction();interactionError=message;publish({});
      void fetchVerification(generation,true);return true;
    }
    function verificationPointerDown(event) {
      if(disposed || !job || state.status!=='verification_required' || interaction || verification.busy || !verificationFrame ||
        verificationFrame.revision!==verification.revision || !verification.ready || event.isPrimary===false || event.button!==0 || event.isTrusted===false)return false;
      const element=event.currentTarget,rect=element?.getBoundingClientRect?.();
      if(!rect || rect.width<=0 || rect.height<=0 || element.complete===false ||
        (element.currentSrc || element.src)!==verificationFrame.url)return false;
      const active={phase:'tracking',attempt:generation,jobId:job.id,revision:verificationFrame.revision,element,pointerId:event.pointerId,
        rect:{left:rect.left,top:rect.top,width:rect.width,height:rect.height},startedAt:pointerTime(event),points:[],travel:0};
      const point=sample(event,active);if(!point)return false;active.points=[point];
      if(verificationRequest)verificationRequest.controller.abort();verificationRequest=null;
      interaction=active;interactionError='';publish({interactionPending:true,gestureActive:true});
      event.preventDefault?.();
      try{element.setPointerCapture?.(event.pointerId);}catch(_){return abortGesture();}
      return true;
    }
    function verificationPointerMove(event) {
      const active=interaction;
      if(!active || active.phase!=='tracking' || event.pointerId!==active.pointerId)return false;
      event.preventDefault?.();const point=sample(event,active);
      if(!point || !appendPoint(active,point))return abortGesture('操作超出图片范围、页面尺寸已变化或超过三秒，请重新操作。');
      return true;
    }
    async function verificationPointerUp(event) {
      const active=interaction;
      if(!active || active.phase!=='tracking' || event.pointerId!==active.pointerId)return false;
      event.preventDefault?.();const point=sample(event,active);
      if(!point || !appendPoint(active,point))return abortGesture('操作超出图片范围、页面尺寸已变化或超过三秒，请重新操作。');
      const action=active.travel>4?'drag':'click';
      const points=action==='click'?[{x:point.x,y:point.y,t:0}]:active.points.length<=64?active.points:
        Array.from({length:64},(_,index)=>active.points[Math.round(index*(active.points.length-1)/63)]);
      active.phase='posting';publish({gestureActive:false});
      try{if(active.element.hasPointerCapture?.(active.pointerId))active.element.releasePointerCapture(active.pointerId);}catch(_){}
      const submittedJob=job;
      try {
        // A genuine pointer gesture is submitted once. Network errors never replay it.
        const response=await api.post(BASE+'/verification-action',{job_id:submittedJob.id,action_id:uuid(),verification_revision:active.revision,action,points},
          {headers:headers(submittedJob,true),timeout:15000});
        if(!current(active.attempt) || interaction!==active || job?.id!==submittedJob.id)return false;
        active.phase='queued';minimumStatusSerial=statusSerial+1;
        apply(response.data,active.attempt);
        if(!interaction && !STOP.has(state.status))refreshVisibleImage(active.attempt,true,response.data?.qr_revision ?? null);
        return true;
      } catch(error) {
        if(!current(active.attempt) || interaction!==active)return false;
        minimumStatusSerial=statusSerial+1;
        const definite=[400,403,404,409,410,422,429].includes(error?.response?.status);
        interactionError=definite?messageFrom(error,'验证图片已变化，请等待新图片后重新操作。'):'操作结果暂未确认，正在更新状态；不会自动重复点击或拖动。';
        if(definite){releaseInteraction();clearVerificationImage();void fetchVerification(active.attempt,true);}
        else active.phase='uncertain';
        publish({interactionPending:!!interaction || verification.busy});
        return false;
      }
    }
    function verificationPointerCancel(event) {
      if(!interaction || interaction.phase!=='tracking' || event.pointerId!==interaction.pointerId)return false;
      return abortGesture();
    }
    window.addEventListener('pagehide',dispose);
    return {start:()=>setup('start'),refresh:()=>setup('refresh'),cancel,dispose,snapshot,retryStart,retrySync,retryImage,retryCleanup,
      verificationPointerDown,verificationPointerMove,verificationPointerUp,verificationPointerCancel};
  }
  window.SparkQrLogin={create};
})();
