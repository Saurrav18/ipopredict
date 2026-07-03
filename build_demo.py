"""
build_demo.py - turn index.html + qualified_ipos.json into a standalone demo.html
that runs with NO server: live IPOs are baked in, and a tiny in-browser layer
fakes signup / OTP / login / alerts / ask so the whole site is clickable offline.

Run:  python build_demo.py
Output: demo.html  (just double-click it to open in a browser)
"""
import json, re, pathlib

HERE = pathlib.Path(__file__).parent
html = (HERE / "index.html").read_text(encoding="utf-8")
live = json.loads((HERE / "qualified_ipos.json").read_text(encoding="utf-8"))

SHIM = r"""
<script>
/* ===== DEMO MODE: baked data + in-browser fake backend (no server needed) ===== */
window.PREVIEW_LIVE = __LIVE__;
(function(){
  var users={}, session=null, CODE="123456";
  var LIVE = window.PREVIEW_LIVE || [];
  function J(o,s){ return Promise.resolve(new Response(JSON.stringify(o),{status:s||200,headers:{"Content-Type":"application/json"}})); }
  function lc(x){ return (x||"").toString().trim().toLowerCase(); }
  var CONCEPTS={
    "gmp":"GMP (grey market premium) is what buyers unofficially pay over the offer price before listing. High GMP hints at demand, but it is unofficial and can vanish by listing day.",
    "confidence":"Confidence is how sure the model is about its APPLY or SKIP call, 0 to 100. Higher means the past pattern was clearer. It is not a guarantee.",
    "p/e":"P/E compares the offer price to the company profit. A very high P/E can mean the IPO is priced expensively versus peers.",
    "pe":"P/E compares the offer price to the company profit. A very high P/E can mean the IPO is priced expensively versus peers.",
    "qib":"QIB is the big-institution portion. Strong QIB subscription (many times over) usually signals serious interest.",
    "tier":"Your tier is a historical hit rate. At the 90% tier, about 9 of 10 IPOs that cleared it listed above offer in the past. Higher tier = fewer but safer alerts.",
    "allotment":"Allotment odds are the rough chance you actually get shares if you apply, based on how oversubscribed the retail portion is."
  };
  function named(q){
    var best=null,bh=0;
    LIVE.forEach(function(x){
      var stop=["ipo","ltd","the","and","solutions","technologies","limited","fintech","leisure","tourism","packaging"];
      var w=lc(x.ipo_name).replace(/[^a-z0-9 ]+/g," ").split(/\s+/).filter(function(t){return t.length>2&&stop.indexOf(t)<0;});
      var hits=w.filter(function(t){return q.indexOf(t)>=0;}).length;
      var lead=w[0]&&w[0].length>=4&&q.indexOf(w[0])>=0;
      if((hits>=1||lead)&&hits>=bh){best=x;bh=Math.max(hits,lead?1:0);}
    });
    return best;
  }
  function demoAsk(q){
    q=lc(q);
    if(/what|explain|mean|tell me about/.test(q)){ for(var k in CONCEPTS){ if(q.indexOf(k)>=0) return {parser:"concept",answer:CONCEPTS[k]}; } }
    if(q.indexOf("apply")>=0&&(q.indexOf("which")>=0||q.indexOf("should")>=0)&&!named(q)){
      var ap=LIVE.filter(function(x){return x.prediction&&x.prediction.verdict==="APPLY";});
      if(!ap.length) return {parser:"rules",answer:"Right now none of the open IPOs are an APPLY on our model."};
      var ln=ap.map(function(x){return "- "+x.ipo_name+": APPLY, confidence "+x.prediction.confidence+(x.preliminary?" (preliminary, subscription not fully in)":"");});
      return {parser:"rules",answer:"Open IPOs our model rates APPLY:\n"+ln.join("\n")+"\n\nInformation only, not investment advice."};
    }
    var h=named(q);
    if(h){
      var p=h.prediction||{}, inp=h.inputs||{};
      if(!p.verdict) return {parser:"rules",answer:h.ipo_name+" is open but we do not have a confident call yet."};
      var s=h.ipo_name+": our model says "+p.verdict+" with confidence "+p.confidence+".";
      if(p.range_low!=null&&p.range_high!=null) s+=" Estimated listing range "+p.range_low+"% to "+p.range_high+"%.";
      if(inp.gmp!=null) s+=" Grey market premium about "+inp.gmp+"%.";
      if(inp.sub!=null) s+=" Subscribed about "+inp.sub+"x.";
      if(h.preliminary) s+=" Note: subscription not fully in yet, so this is preliminary.";
      s+=" Information only, not investment advice.";
      return {parser:"rules",answer:s};
    }
    return {parser:"",answer:"This is the demo question box. Try 'what is GMP', 'should I apply to Advit', or 'which IPOs should I apply to'. The live site runs the full research engine."};
  }
  var realFetch=window.fetch.bind(window);
  window.fetch=function(url,opts){
    opts=opts||{}; var m=(opts.method||"GET").toUpperCase(); var b={};
    try{ b=opts.body?JSON.parse(opts.body):{}; }catch(e){}
    var u=(""+url).split("?")[0]; var has=function(s){return u.indexOf(s)>=0;};
    if(has("/auth/me")) return J(session?{authenticated:true,user:{email:session.email,tier:session.tier,telegram:session.telegram}}:{authenticated:false});
    if(has("/auth/signup")){ var e=lc(b.email);
      if(!e||e.indexOf("@")<0) return J({detail:"Enter a valid email."},400);
      if((b.password||"").length<8) return J({detail:"Password must be at least 8 characters."},400);
      if(users[e]&&users[e].verified) return J({detail:"That email is already registered. Try signing in."},409);
      users[e]={password:b.password,verified:false,tier:90,telegram:null}; return J({ok:true,emailed:false,dev_otp:CODE}); }
    if(has("/auth/verify-otp")){ var e2=lc(b.email);
      if(b.code!==CODE) return J({detail:"Incorrect code. In this demo the code is always 123456."},400);
      if(users[e2]) users[e2].verified=true; else users[e2]={password:"",verified:true,tier:90,telegram:null};
      session={email:e2,tier:users[e2].tier||90,telegram:users[e2].telegram||null}; return J({ok:true,authenticated:true}); }
    if(has("/auth/login")){ var e3=lc(b.email),us=users[e3];
      if(!us||us.password!==b.password) return J({detail:"Wrong email or password."},401);
      if(!us.verified) return J({detail:"Please verify your email first."},403);
      session={email:e3,tier:us.tier||90,telegram:us.telegram||null}; return J({ok:true,authenticated:true}); }
    if(has("/auth/resend-otp")) return J({ok:true,emailed:false,dev_otp:CODE});
    if(has("/auth/forgot")){ var e4=lc(b.email); if(users[e4]) return J({ok:true,emailed:false,dev_otp:CODE}); return J({ok:true}); }
    if(has("/auth/reset")){ var e5=lc(b.email);
      if(b.code!==CODE) return J({detail:"Incorrect code. In this demo the code is always 123456."},400);
      if((b.password||"").length<8) return J({detail:"Password must be at least 8 characters."},400);
      if(users[e5]) users[e5].password=b.password; else users[e5]={password:b.password,verified:true,tier:90,telegram:null};
      session={email:e5,tier:users[e5].tier||90,telegram:null}; return J({ok:true,authenticated:true}); }
    if(has("/auth/logout")){ session=null; return J({ok:true}); }
    if(has("/account/unsubscribe")){ if(session){ session.tier=null; if(users[session.email]) users[session.email].tier=null; } return J({ok:true,unsubscribed:true}); }
    if(has("/account")){ if(m==="POST"){ if(session){ session.tier=b.tier||session.tier; session.telegram=b.telegram||""; if(users[session.email]){users[session.email].tier=session.tier;users[session.email].telegram=session.telegram;} } return J({ok:true}); } return J(session?{tier:session.tier,telegram:session.telegram}:{}); }
    if(has("/subscribe")){ if(session){ session.tier=b.tier||session.tier; session.telegram=b.telegram||""; } return J({ok:true}); }
    if(has("/ask")) return J(demoAsk(b.question||b.q||""));
    return realFetch(url,opts);
  };
  function banner(){ if(document.getElementById("demoBar"))return;
    var d=document.createElement("div"); d.id="demoBar";
    d.innerHTML="<span>Demo &middot; code is always <strong>123456</strong> &middot; nothing saved</span><button id='demoX' aria-label='Hide' style='background:none;border:none;color:#fff;font:700 16px/1 sans-serif;cursor:pointer;padding:0 4px;margin-left:8px'>&times;</button>";
    d.style.cssText="position:sticky;top:0;z-index:9999;background:#8a6d00;color:#fff;font:600 11px/1.4 'JetBrains Mono',monospace;padding:5px 12px;display:flex;align-items:center;justify-content:center;gap:4px";
    document.body.insertBefore(d,document.body.firstChild);
    var x=document.getElementById("demoX"); if(x) x.onclick=function(){ d.remove(); }; }
  if(document.readyState!=="loading") banner(); else document.addEventListener("DOMContentLoaded",banner);
})();
</script>
"""

shim = SHIM.replace("__LIVE__", json.dumps(live, ensure_ascii=False))
out = re.sub(r"(<head[^>]*>)", lambda mm: mm.group(1) + "\n" + shim, html, count=1)
(HERE / "demo.html").write_text(out, encoding="utf-8")
print(f"demo.html written ({len(out)} bytes) with {len(live)} IPOs baked in")
