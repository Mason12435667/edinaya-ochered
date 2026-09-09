"""Display identity only: never change WhatsApp IDs, routing or original text."""
import re
import unicodedata


def clean(value):
    name=''.join(c for c in str(value or '') if unicodedata.category(c) not in {'Cc','Cs'}).strip()
    if not name or '\ufffd' in name or not any(not c.isspace() and unicodedata.category(c) != 'Cf' for c in name): return ''
    if name.casefold() in {'система','system','рабочий whatsapp','пользователь whatsapp','участник','участник группы','direct','none','null','undefined'}: return ''
    if re.fullmatch(r'[+\d\s().-]+',name) or re.search(r'@(lid|c\.us|g\.us)$',name): return ''
    return name[:100]


def phone_key(value):
    text=str(value or '').strip()
    if '@' in text:
        if not text.endswith('@c.us'):return ''
        text=text[:-5]
    if re.search(r'[^+\d\s().-]',text):return ''
    digits=re.sub(r'\D','',text)
    if len(digits)==11 and digits.startswith('8'):digits='7'+digits[1:]
    return digits+'@c.us' if 7<=len(digits)<=15 else ''


class Names:
    def __init__(self,manual,records):
        self.parents={};self.manual={};self.known={}
        rows=[*records,*manual]
        for row in rows:
            keys=self.keys(row)
            for key in keys:self.parents.setdefault(key,key)
            for key in keys[1:]:self.parents[self.root(key)]=self.root(keys[0])
        for row in records:
            name=clean(row.get('name'))
            for key in self.keys(row):
                if name:self.known[self.root(key)]=name
        for row in manual:
            name=str(row.get('name') or '').strip()[:100]
            for key in self.keys(row):
                if name:self.manual[self.root(key)]=name

    @staticmethod
    def keys(row):
        keys=[]
        for field in ('chat_id','id','mention_id','resolved_id'):
            value=str(row.get(field) or '').strip()
            if re.fullmatch(r'[\w.:-]+@(c\.us|lid)',value):keys.append(value)
        value=phone_key(row.get('phone') or row.get('sender_phone'))
        if value:keys.append(value)
        return list(dict.fromkeys(keys))

    def root(self,key):
        while key in self.parents and self.parents[key]!=key:
            self.parents[key]=self.parents.get(self.parents[key],self.parents[key]);key=self.parents[key]
        return key

    def name(self,identity='',current='',phone='',aliases=()):
        keys=[str(identity),*map(str,aliases)]
        if phone_key(phone):keys.append(phone_key(phone))
        roots=[self.root(k) for k in keys if k]
        for r in roots:
            if r in self.manual:return self.manual[r]
        if clean(current):return clean(current)
        for r in roots:
            if r in self.known:return self.known[r]
        number=phone_key(phone) or phone_key(identity)
        if number:return '+'+number.split('@')[0]
        return 'Контакт без имени'
