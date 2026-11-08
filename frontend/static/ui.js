/* Shared accessible media controls and opt-in notification audio. */
(() => {
  "use strict";
  const icons = {
    play: '<path d="m9 5 11 7-11 7z"/>',
    pause: '<path d="M7 5h3v14H7zM14 5h3v14h-3z"/>',
    volume: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 8c3 2 3 6 0 8" fill="none" stroke="currentColor" stroke-width="2"/>',
    mute: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="m17 9 5 6m0-6-5 6" fill="none" stroke="currentColor" stroke-width="2"/>',
    expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" fill="none" stroke="currentColor" stroke-width="2"/>',
  };
  const svg = name => `<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">${icons[name]}</svg>`;
  const clock = value => Number.isFinite(value) ? `${Math.floor(value/60)}:${String(Math.floor(value%60)).padStart(2,"0")}` : "0:00";
  function button(label, icon) {
    const result = document.createElement("button");
    result.type = "button"; result.className = "player-button";
    result.title = label; result.setAttribute("aria-label",label);
    if (icon) result.innerHTML = svg(icon);
    return result;
  }
  function enhance(root) {
    root.querySelectorAll("audio.chat-media-audio, video.chat-media-video, audio.ticket-media-audio, video.ticket-media-video").forEach(media => {
      if (media.dataset.playerReady) return;
      media.dataset.playerReady = "1";
      const isVideo = media.tagName === "VIDEO";
      const frame = document.createElement("div");
      frame.className = `queue-player ${isVideo ? "video-player" : "audio-player"}`;
      media.before(frame); frame.append(media);
      media.controls = false;
      if (!isVideo) media.hidden = true;
      const controls = document.createElement("div"); controls.className = "player-controls";
      const play = button("Воспроизвести", "play"); play.classList.add("player-play");
      const track = document.createElement("input");
      track.type = "range"; track.min = "0"; track.max = "1000"; track.value = "0"; track.step = "1";
      track.className = "player-seek"; track.setAttribute("aria-label","Позиция воспроизведения");
      const time = document.createElement("span"); time.className = "player-time"; time.textContent = "0:00 / 0:00";
      const speed = button("Скорость воспроизведения"); speed.textContent = "1×";
      const volume = button("Выключить звук", "volume");
      const status = document.createElement("small"); status.className = "player-status"; status.setAttribute("role","status"); status.hidden = true;
      const showError = () => { status.hidden = false; status.textContent = "Не удалось воспроизвести. Попробуйте скачать файл."; };
      async function toggle() {
        if (media.paused) {try {await media.play();} catch (_) {showError();}} else media.pause();
      }
      play.addEventListener("click",toggle);
      if (isVideo) media.addEventListener("click",toggle);
      media.addEventListener("play", () => {
        document.querySelectorAll("audio,video").forEach(other => {if (other !== media) other.pause();});
        play.innerHTML = svg("pause"); play.setAttribute("aria-label","Пауза"); play.title="Пауза";
        frame.classList.add("playing"); status.hidden=true;
      });
      media.addEventListener("pause", () => {
        play.innerHTML=svg("play"); play.setAttribute("aria-label","Воспроизвести"); play.title="Воспроизвести";
        frame.classList.remove("playing");
      });
      media.addEventListener("error",showError);
      function progress() {
        const duration = media.duration;
        track.disabled = !Number.isFinite(duration) || duration <= 0;
        const value = !track.disabled ? Math.round(media.currentTime/duration*1000) : 0;
        track.value=String(value); track.style.setProperty("--progress",`${value/10}%`);
        time.textContent=`${clock(media.currentTime)} / ${clock(duration)}`;
        track.setAttribute("aria-valuetext",`${clock(media.currentTime)} из ${clock(duration)}`);
      }
      media.addEventListener("timeupdate",progress);
      media.addEventListener("loadedmetadata",progress);
      media.addEventListener("durationchange",progress);
      track.addEventListener("input", () => {if (Number.isFinite(media.duration)) media.currentTime=Number(track.value)/1000*media.duration;});
      speed.addEventListener("click", () => {
        const values=[1,1.5,2,0.75]; media.playbackRate=values[(values.indexOf(media.playbackRate)+1)%values.length];
        speed.textContent=media.playbackRate+"×";
      });
      volume.addEventListener("click", () => {
        media.muted=!media.muted; volume.innerHTML=svg(media.muted?"mute":"volume");
        volume.title=media.muted?"Включить звук":"Выключить звук"; volume.setAttribute("aria-label",volume.title);
      });
      const timeline=document.createElement("div"); timeline.className="player-timeline"; timeline.append(track,time);
      controls.append(play,timeline,speed,volume);
      if (isVideo && frame.requestFullscreen) {
        const full=button("Полный экран","expand");
        full.addEventListener("click",async()=>{try{if(document.fullscreenElement) await document.exitFullscreen();else await frame.requestFullscreen();}catch(_) {}});
        controls.append(full);
      }
      const download=document.createElement("a");
      const url=new URL(media.src,window.location.href); url.searchParams.set("download","1");
      download.href=url.href; download.className="player-download"; download.textContent="Скачать";
      frame.append(controls,status,download); progress();
    });
  }
  let audioContext=null, lastSound=0, enabled=true;
  try {
    const savedSound=localStorage.getItem("queue-notification-sound");
    enabled=savedSound===null ? true : savedSound==="1";
  } catch (_) {}
  function unlock() {
    if (!enabled) return;
    const Audio=window.AudioContext||window.webkitAudioContext;
    if (!Audio) return;
    if (!audioContext) audioContext=new Audio();
    if (audioContext.state==="suspended") audioContext.resume().catch(()=>{});
  }
  function playNotice() {
    if (!enabled || Date.now()-lastSound<1800 || !audioContext || audioContext.state!=="running") return;
    lastSound=Date.now();
    [740,988,1318].forEach((frequency,index)=>{
      const oscillator=audioContext.createOscillator(), gain=audioContext.createGain();
      const start=audioContext.currentTime+index*.14;
      oscillator.type="sine"; oscillator.frequency.value=frequency;
      gain.gain.setValueAtTime(0,start);
      gain.gain.linearRampToValueAtTime(.18,start+.018);
      gain.gain.exponentialRampToValueAtTime(.001,start+.27);
      oscillator.connect(gain); gain.connect(audioContext.destination); oscillator.start(start); oscillator.stop(start+.29);
      oscillator.onended=()=>{oscillator.disconnect();gain.disconnect();};
    });
  }
  function soundToggle(parent) {
    const toggle=button("Звук уведомлений"); toggle.className="notification-sound";
    function label() {toggle.textContent=enabled?"Звук уведомлений: громкий":"Включить звук уведомлений";toggle.setAttribute("aria-pressed",String(enabled));}
    label(); toggle.addEventListener("click",()=>{
      enabled=!enabled; try{localStorage.setItem("queue-notification-sound",enabled?"1":"0");}catch(_){}
      label(); unlock(); if(enabled) window.setTimeout(playNotice,120);
    }); parent.append(toggle);
  }
  document.addEventListener("click",unlock,{passive:true});
  window.QueueUI={enhance,playNotice,soundToggle};
  document.addEventListener("DOMContentLoaded",()=>enhance(document));
})();
