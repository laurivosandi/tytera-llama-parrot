// SPDX-License-Identifier: GPL-3.0-or-later
const $ = id => document.getElementById(id);
const entries = new Map();
let paused = false;
const phases = {'waiting for channel':'Waiting for a quiet channel…',starting:'Starting up',listening:'Listening to the radio',thinking:'Siil is thinking…',transcribing:'Listening closely…',synthesizing:'Finding his voice…',transmitting:'Siil is on the air',stopped:'Bot stopped',error:'Something needs attention'};
function node(tag, cls, text) { const n=document.createElement(tag); if(cls)n.className=cls; if(text)n.textContent=text; return n; }
function row(turn, outgoing) {
 const r=node('div', 'row'+(outgoing?' out':''));
 const avatar=node('div','avatar');
 if(outgoing){const img=node('img');img.src='/icon.svg';img.alt='Siil';avatar.append(img);}else avatar.textContent='↗';
 const msg=node('div','message');msg.append(node('div','speaker',outgoing?'Siil Udus':'Caller'));
 const bubble=node('div','bubble',outgoing?turn.reply:(turn.transcript||(turn.phase==='recording'?'Recording transmission…':'Processing transmission…')));
 const file=outgoing?'reply':'received';const base='/recordings/'+encodeURIComponent(turn.id)+'/';
 if(outgoing?turn.audio:turn.received){const audio=node('audio');audio.controls=true;audio.preload='none';audio.src=base+file+'.wav';bubble.append(audio);}
 msg.append(bubble);const links=node('div','links');
 if(outgoing?turn.audio:turn.received){const a=node('a','','WAV ↗');a.href=base+file+'.wav';a.download=file+'.wav';links.append(a);}
 if(outgoing?turn.reply:turn.transcript){const a=node('a','','TXT ↗');a.href=base+(outgoing?'reply':'transcript')+'.txt';a.download=true;links.append(a);}
 if(outgoing&&turn.language)links.append(node('span','',turn.language.toUpperCase()));
 if(outgoing&&turn.processing_seconds!=null)links.append(node('span','',turn.processing_seconds+' s'));
 msg.append(links);r.append(avatar,msg);return r;
}
function render(data){
 const s=data.status;paused=s.paused;$('pause').textContent=paused?'Resume bot':'Pause bot';
 $('phase').textContent=(paused?'Paused · ':'')+(phases[s.phase]||s.phase);
 $('level').textContent=s.level||0;$('meter').value=s.level||0;$('buffer').textContent=(s.buffer_seconds||0)+' / '+(s.window_seconds||30)+' s';
 if(s.channel){$('channel').textContent=s.channel.name||'Radio';$('frequency').textContent=(s.channel.rx/1e6).toFixed(5)+' MHz';}else if(s.channel_error){$('frequency').textContent=s.channel_error;}
 $('mode').textContent=s.mode||'';$('tx').textContent=s.transmit?'Radio replies enabled':'Generate audio only';
 $('error').hidden=!s.error;if(s.error)$('error').textContent=s.error;
 $('reply').disabled=paused||!['listening','thinking','synthesizing'].includes(s.phase);
 const panel=$('conversation');const bottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<100;
 $('empty').hidden=data.turns.length>0; if(data.turns.length)$('empty').style.display='none';
 for(const turn of data.turns){
   const signature=JSON.stringify(turn);let entry=entries.get(turn.id);
   if(entry&&entry.signature===signature)continue;
   if(!entry){entry={element:node('article','turn')};entries.set(turn.id,entry);panel.append(entry.element);}
   entry.signature=signature;
   // Update the two bubbles independently, preserving playing audio in the other bubble.
   if(!entry.stamp){entry.stamp=node('div','stamp',new Date(turn.timestamp).toLocaleString());entry.element.append(entry.stamp);}
   const inputSignature=JSON.stringify([turn.transcript,turn.received,turn.phase]);
   if(entry.inputSignature!==inputSignature){const input=row(turn,false);if(entry.input)entry.input.replaceWith(input);else entry.element.append(input);entry.input=input;entry.inputSignature=inputSignature;}
   const outputSignature=JSON.stringify([turn.reply,turn.audio,turn.language,turn.processing_seconds]);
   if(turn.reply&&entry.outputSignature!==outputSignature){const output=row(turn,true);if(entry.output)entry.output.replaceWith(output);else entry.element.append(output);entry.output=output;entry.outputSignature=outputSignature;}
   if(turn.error&&!entry.error){entry.error=node('div','error',turn.error);entry.element.append(entry.error);}
 }
 if(bottom)panel.scrollTop=panel.scrollHeight;
}
async function command(action){try{const r=await fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});if(!r.ok)throw Error('Control request failed');}catch(e){$('error').textContent=e.message;$('error').hidden=false;}}
$('pause').onclick=()=>command(paused?'resume':'pause');$('reply').onclick=()=>command('reply');
const events=new EventSource('/events');events.onopen=()=>{$('connection').textContent='Live';$('connection').classList.remove('offline');};events.onerror=()=>{$('connection').textContent='Reconnecting…';$('connection').classList.add('offline');};events.addEventListener('state',event=>render(JSON.parse(event.data)));
