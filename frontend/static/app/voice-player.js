// Постоянный медиаплеер: один на страницу и не зависит от перерисовки чата.
window.QueueVoice = (() => {
  const audio = new Audio(); audio.preload = "metadata";
  let panel, source, seek, clock, playButton;
  const format = n => `${Math.floor((n || 0)/60)}:${String(Math.floor((n || 0)%60)).padStart(2,"0")}`;
  function ensure() {
    if (panel) return;
    panel = document.createElement("section"); panel.className = "queue-voice-dock"; panel.setAttribute("aria-label", "Голосовое сообщение");
    const make = (text, action) => { const b=document.createElement("button"); b.type="button"; b.textContent=text; b.onclick=action; panel.append(b); return b; };
    playButton=make("Пауза", () => audio.paused ? audio.play().catch(fail) : audio.pause());
    make("Стоп", () => { audio.pause(); audio.currentTime=0; });
    seek=document.createElement("input"); seek.type="range"; seek.min="0"; seek.max="100"; seek.value="0"; seek.step="0.1"; seek.setAttribute("aria-label","Позиция воспроизведения");
    seek.oninput=()=>{if(Number.isFinite(audio.duration)) audio.currentTime=Number(seek.value)/100*audio.duration;}; panel.append(seek);
    clock=document.createElement("span"); panel.append(clock);
    const speed=document.createElement("select"); speed.setAttribute("aria-label","Скорость воспроизведения");
    [1,1.5,2].forEach(v=>{const o=document.createElement("option"); o.value=v;o.textContent=v+"×";speed.append(o);}); speed.onchange=()=>audio.playbackRate=Number(speed.value); panel.append(speed);
    source=document.createElement("a");source.textContent="К сообщению";panel.append(source);
    make("Закрыть",()=>{audio.pause();panel.hidden=true;});document.body.append(panel);
  }
  function fail(){ if(clock) clock.textContent="Аудио недоступно"; }
  audio.addEventListener("error",fail);
  ["play","pause","timeupdate","loadedmetadata","ended"].forEach(e=>audio.addEventListener(e,()=>{
    if(!panel)return;playButton.textContent=audio.paused?"Продолжить":"Пауза";
    clock.textContent=format(audio.currentTime)+" / "+format(Number.isFinite(audio.duration)?audio.duration:0);
    seek.value=Number.isFinite(audio.duration)&&audio.duration>0?audio.currentTime/audio.duration*100:0;
  }));
  return {play(message,chat){
    ensure(); panel.hidden=false;
    document.querySelectorAll("audio,video").forEach(m=>m.pause());
    const url=new URL(message.media_url,location.origin).href;
    if(audio.src!==url){audio.src=url;audio.currentTime=0;}
    source.href=(chat.endsWith("@g.us")?"/groups":"/whatsapp")+"?chat_id="+encodeURIComponent(chat)+"&message_id="+encodeURIComponent(message.id);
    source.onclick=e=>{if(new URL(source.href).pathname===location.pathname){e.preventDefault();history.replaceState({},"",source.href);window.dispatchEvent(new Event("queue-open-message"));}};
    audio.play().catch(fail);
  }};
})();
