// Sample fridge contents, grouped into shelves, shared by all 5 mockups.
const TONE = {
  hot:   ['var(--hot)',   'var(--hot-bg)'],
  warm:  ['var(--warm)',  'var(--warm-bg)'],
  cool:  ['var(--cool)',  'var(--cool-bg)'],
  staple:['var(--staple)','var(--staple-bg)'],
};
const SHELVES = [
  { icon:'⏳', title:'Bald essen', items:[
    { emoji:'🍗', name:'Hähnchenbrust', meta:'400 g', loc:'Kühlschrank', due:'heute',   band:'hot', overdue:true },
    { emoji:'🥛', name:'Vollmilch 3,5%', meta:'1,5 L', loc:'Kühlschrank', due:'morgen', band:'hot' },
    { emoji:'🥗', name:'Feldsalat',      meta:'150 g', loc:'Gemüsefach',  due:'in 2 T.', band:'warm' },
  ]},
  { icon:'🧊', title:'Kühlschrank', items:[
    { emoji:'🧀', name:'Gouda jung',     meta:'200 g',  loc:'Kühlschrank', due:'in 5 T.',   band:'cool' },
    { emoji:'🥚', name:'Eier · Freiland', meta:'6 Stück', loc:'Kühlschrank', due:'in 1 Wo.', band:'cool' },
    { emoji:'🧈', name:'Butter',          meta:'250 g',  loc:'Kühlschrank', due:'Vorrat',    band:'staple' },
  ]},
];
function cardHTML(it){
  const [c,cbg] = TONE[it.band];
  return `<div class="item tabpad" style="--c:${c};--cbg:${cbg}">
    <div class="loc-tab">${it.loc}</div>
    <div class="thumb">${it.emoji}</div>
    <div class="body"><div class="name">${it.name}</div><div class="meta">${it.meta}</div></div>
    <div class="due${it.overdue?' overdue':''}">${it.due}</div>
  </div>`;
}
function renderShelves(el){
  el.innerHTML = SHELVES.map(s => `
    <section class="shelf">
      <div class="shelf-head">
        <span class="sicon">${s.icon}</span>
        <h2>${s.title}</h2>
        <span class="count">${s.items.length}</span>
      </div>
      <div class="items">${s.items.map(cardHTML).join('')}</div>
    </section>`).join('');
}
document.addEventListener('DOMContentLoaded', () => renderShelves(document.getElementById('shelves')));
