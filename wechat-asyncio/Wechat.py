# coding=utf-8


from HttpClient import HttpClient
from datetime import datetime, timedelta,timezone
import aiohttp
import asyncio
import time
import re
import threading
import xml.dom.minidom
import json
import html
import requests
import mimetypes
import os

import config
import logging
import random
from requests_toolbelt.multipart.encoder import MultipartEncoder

logger = logging.getLogger('wx')

class Wechat():
    def __init__(self, client):
        self.__wxclient = HttpClient(client)
        self.__client = client
        self.tip = 0
        self.deviceId = 'e000701000000000'

        self.recvqueue = asyncio.Queue()
        self.sendqueue = asyncio.Queue()
        self.blacklist = []
        self.updatequeue = asyncio.Queue() # 更新群组信息的请求
        self.grouplist = {} # 存储群组的联系人信息
        # 给 monitor 用
        self.retcode = '0'
        self.selector = '0'
        self.media_count = -1

    async def __getuuid(self):
        logger.debug('Entering getuuid.')
        url = 'https://login.weixin.qq.com/jslogin'
        payload = {
            'appid': 'wx782c26e4c19acffb',
            'fun': 'new',
            'lang': 'zh_CN',
            '_': int(time.time()),
        }

        text = await self.__wxclient.post(url=url, data=payload)
        if text == None:
            return False
        logger.info(text)

        regx = r'window.QRLogin.code = (\d+); window.QRLogin.uuid = "(\S+?)"'
        pm = re.search(regx, text)

        code = pm.group(1)
        uuid = pm.group(2)

        self.uuid = uuid
        if code == '200':
            return True
        else:
            return False

    async def __downloadQR(self):
        logger.debug('Entering downloadQR.')
        url = 'https://login.weixin.qq.com/qrcode/' + self.uuid
        payload = {
            't': 'webwx',
            '_': int(time.time()),
        }

        logger.debug("start download qrcode")
        su = await self.__wxclient.downloadfile(url, data=payload, filename='qrimage.jpg')
        logger.debug("start qrcode")
        t=threading.Thread(target=self.show_qrcode, args=('qrimage.jpg',))
        logger.debug("show qrcode")
        t.setDaemon(False)
        logger.debug("show qrcode1")
        t.start()
        logger.debug("show qrcode2")

        logger.info('请扫描二维码')
        print ('请扫描二维码')
        return su
    def show_qrcode(self, path):
        logger.debug("excuting show_qrcode")
        from PIL import ImageTk, Image
        logger.debug("open qrcode")
        logger.debug(path)
        img = Image.open(path)
        img.show()

    async def __waitforlogin(self):
        logger.debug('Waiting for login.......')
        url = 'https://login.weixin.qq.com/cgi-bin/mmwebwx-bin/login?tip=%s&uuid=%s&_=%s' % (self.tip, self.uuid, int(time.time()))
        text = await self.__wxclient.get(url)

        regx = r'window.code=(\d+);'
        pm = re.search(regx, text)
        code = pm.group(1)

        if code == '201':
            logger.info ('成功扫描,请在手机上点击确认以登录')
            print ('成功扫描,请在手机上点击确认以登录')
            self.tip = 0
        elif code == '200':
            logger.info ('正在登录。。。')
            print ('正在登录。。。')
            regx = r'window.redirect_uri="(\S+?)";'
            pm = re.search(regx, text)
            redirect_uri = pm.group(1) + '&fun=new'
            self.redirect_uri  = redirect_uri
            base_uri = redirect_uri[:redirect_uri.rfind('/')]
            self.base_uri = base_uri

            services = [
                ('wx2.qq.com', 'webpush2.weixin.qq.com'),
                ('qq.com', 'webpush.weixin.qq.com'),
                ('web1.wechat.com', 'webpush1.wechat.com'),
                ('web2.wechat.com', 'webpush2.wechat.com'),
                ('wechat.com', 'webpush.wechat.com'),
                ('web1.wechatapp.com', 'webpush1.wechatapp.com'),
            ]

            push_uri = base_uri
            for (searchUrl, pushUrl) in services:
                if base_uri.find(searchUrl) >= 0:
                    push_uri = 'https://%s/cgi-bin/mmwebwx-bin' % pushUrl
                    break
            self.push_uri = push_uri
        elif code == '408':
            pass

        return code


    async def __checklogin(self):
        logger.debug('Entering checklogin.')
        text = await self.__wxclient.get(self.redirect_uri)

        doc = xml.dom.minidom.parseString(text)
        root = doc.documentElement

        for node in root.childNodes:
            if node.nodeName == 'skey':
                skey = node.childNodes[0].data
            elif node.nodeName == 'wxsid':
                wxsid = node.childNodes[0].data
            elif node.nodeName == 'wxuin':
                wxuin = node.childNodes[0].data
            elif node.nodeName == 'pass_ticket':
                pass_ticket = node.childNodes[0].data

        if not all((skey, wxsid, wxuin, pass_ticket)):
            return False

        BaseRequest = {
            'Uin': int(wxuin),
            'Sid': wxsid,
            'Skey': skey,
            'DeviceID': self.deviceId,
        }
        logger.debug('%s, %s, %s, %s', skey, wxsid, wxuin, pass_ticket)
        self.skey = skey
        self.wxsid = wxsid
        self.wxuin = wxuin
        self.pass_ticket = pass_ticket
        self.BaseRequest = BaseRequest

        return True


    async def __responseState(self, func, BaseResponse):
        ErrMsg = BaseResponse['ErrMsg']
        Ret = BaseResponse['Ret']
        logger.info('func: %s, Ret: %d, ErrMsg: %s' % (func, Ret, ErrMsg))
        if Ret != 0:
            return False
        return True


    async def __webwxinit(self):
        logger.debug('Entering webwxinit.')
        url = self.base_uri + \
            '/webwxinit?pass_ticket=%s&skey=%s&r=%s' % (
                self.pass_ticket, self.skey, int(time.time()))
        payload = {
            'BaseRequest' : self.BaseRequest
        }

        dic = await self.__wxclient.post_json(url=url, data=json.dumps(payload))

        self.My = dic['User']
        self.SyncKey = dic['SyncKey']
        logger.debug('The new SyncKey is: %s' % self.SyncKey)

        return await self.__responseState('webwxinit', dic['BaseResponse'])

    async def __webwxgetcontact(self):
        url = self.base_uri + \
        '/webwxgetcontact?pass_ticket=%s&skey=%s&r=%s' % (
            self.pass_ticket, self.skey, int(time.time()))

        dic = await self.__wxclient.get_json(url)

        SpecialUsers = ["newsapp", "fmessage", "filehelper", "weibo", "qqmail", "tmessage", "qmessage", "qqsync", "floatbottle", "lbsapp", "shakeapp", "medianote", "qqfriend", "readerapp", "blogapp", "facebookapp", "masssendapp",
                    "meishiapp", "feedsapp", "voip", "blogappweixin", "weixin", "brandsessionholder", "weixinreminder", "wxid_novlwrv3lqwv11", "gh_22b87fa7cb3c", "officialaccounts", "notification_messages", "wxitil", "userexperience_alarm"]
        self.blacklist += SpecialUsers

        MemberList = {}
        for member in dic['MemberList']:
            if member['VerifyFlag'] & 8 != 0: # 公众号
                continue
            elif member['UserName'] in SpecialUsers:
                continue
            MemberList[member['UserName']] = {
                'NickName' : member['NickName'] ,
                'DisplayName' : member['DisplayName']
            }

        self.memberlist = MemberList
        logger.info('You have %s friends.' % len(MemberList))



    async def __login(self):
        success = await self.__getuuid()
        if not success:
            logger.info ('获取 uuid 失败')
            print ('获取 uuid 失败')
        success = await self.__downloadQR()
        if not success:
            logger.info ('获取二维码失败')
            print ('获取二维码失败')

        while await self.__waitforlogin() != '200':
            pass

        success = await self.__checklogin()
        if not success:
            logger.info ('登陆失败')
            print ('登陆失败')
        logger.info ('登陆成功')
        print ('登陆成功')
        success = await self.__webwxinit()
        if not success:
            logger.info ('初始化失败')
            print ('初始化失败')
        logger.info ('初始化成功')
        print ('初始化成功')

        await self.__webwxgetcontact()

    def __syncKey(self):
        SyncKey = self.SyncKey
        SyncKeyItems = ['%s_%s' % (item['Key'], item['Val'])
                        for item in SyncKey['List']]
        SyncKeyStr = '|'.join(SyncKeyItems)
        return SyncKeyStr

    async def __synccheck(self):
        url = self.push_uri + '/synccheck?'
        BaseRequest = self.BaseRequest
        params = {
            'skey': BaseRequest['Skey'] ,
            'sid': BaseRequest['Sid'] ,
            'uin': BaseRequest['Uin'] ,
            'deviceId': BaseRequest['DeviceID'] ,
            'synckey': self.__syncKey() ,
            'r': int(time.time()*1000)
        }
        text = await self.__wxclient.get(url, params = params)
        if text == None or text == '':
            return ('1111', '1111')

        regx = r'window.synccheck={retcode:"(\d+)",selector:"(\d+)"}'
        pm = re.search(regx, text)

        retcode = pm.group(1)
        selector = pm.group(2)
        logger.info('retcode: %s, selector: %s' % (retcode, selector))
        return (retcode, selector)

    async def __webwxsync(self):
        url = self.base_uri + '/webwxsync?'
        payload = {
            'BaseRequest' : self.BaseRequest ,
            'SyncKey' : self.SyncKey ,
            'rr' : ~int(time.time())
        }
        params = {
            'skey' : self.BaseRequest['Skey'] ,
            'pass_ticket' : self.pass_ticket ,
            'sid' : self.BaseRequest['Sid']
        }

        dic = await self.__wxclient.post_json(url, params=params, data=json.dumps(payload))
        if dic == None:
            return
        # 更新 synckey
        self.SyncKey = dic['SyncKey']

        await self.__responseState('webwxsync', dic['BaseResponse'])

        msglist = dic['AddMsgList']
        for msg in msglist:
            logger.debug(msg)
            await self.recvqueue.put(msg)


    async def __webwxsendmsg(self, content, user, Type=1):
        url = self.base_uri + \
            '/webwxsendmsg?pass_ticket=%s' % (self.pass_ticket)

        msgid = int(time.time()*10000000)
        msg = {
            'ClientMsgId' : msgid ,
            'Content' : content,
            'FromUserName' : self.My['UserName'] ,
            'LocalID' : msgid ,
            'ToUserName' : user,
            'Type' : Type
        }
        payload = {
            'BaseRequest' : self.BaseRequest ,
            'Msg' : msg
        }
        data = json.dumps(payload, ensure_ascii=False)
        data = data.encode('utf-8')

        text = await self.__wxclient.post(url, data=data)


    async def __webwxbatchgetcontact(self, groupname):
        url = self.base_uri + '/webwxbatchgetcontact?'
        List = [{
            'ChatRoomId' : '',
            'UserName' : groupname
        }]
        payload = {
            'BaseRequest': self.BaseRequest ,
            'Count' : 1 ,
            'List' : List
        }
        params = {
            'lang' : 'zh_CN' ,
            'type' : 'ex' ,
            'pass_ticket' : self.pass_ticket ,
            'r' : int(time.time())
        }

        dic = await self.__wxclient.post_json(url, params=params, data=json.dumps(payload))
        if dic == None:
            return
        GroupMapUsers = {}
        ContactList = dic['ContactList']
        for contact in ContactList:
            memberlist = contact['MemberList']
            for member in memberlist:
                # 默认 @群名片，没有群名片就 @昵称
                nickname = member['NickName']
                displayname = member['DisplayName']
                AT = ''
                if displayname == '':
                    # 有些人的昵称会有表情 <span> 会表示成 &lt;span&gt;
                    # 需要 html.unescape() 转义一下
                    AT = html.unescape(nickname)
                else:
                    AT = html.unescape(displayname)
                GroupMapUsers[member['UserName']] = AT

        self.grouplist[groupname] = GroupMapUsers

    async def sync(self):
        await self.__login()
        logger.info ('开始心跳噗通噗咚 咚咚咚！！！！')
        print ('开始心跳噗通噗咚 咚咚咚！！！！')
        logger.info('Begin to sync with wx server.....')
        while True:
            retcode, selector = await self.__synccheck()
            if retcode != '0':
                logger.info ('sync 失败')
                print ('sync 失败')
            if selector != '0':
                await self.__webwxsync()

            await asyncio.sleep(config.sync_interval)
            self.retcode = retcode
            self.selector = selector


    async def sendmsg(self):
        while True:
            response = await self.sendqueue.get()
            # 不要发的太频繁，在拿到 response 之后歇一秒
            await asyncio.sleep(config.send_interval)
            await self.__webwxsendmsg(response['Content'], response['user'], response['MsgType'])

    async def updategroupinfo(self):
        while True:
            groupname = await self.updatequeue.get()

            logger.info('更新群信息开始')
            await self.__webwxbatchgetcontact(groupname)
            await asyncio.sleep(config.updategroupinfo_interval)
            logger.info('更新群信息结束')

    def getUSerID(self, name):
        for member in self.MemberList:
            if name == member['RemarkName'] or name == member['NickName']:
                return member['UserName']
        return None
    async def sendImg(self, user_id, file_name):
        #user_id = self.getUSerID(name)
        response = await self.webwxuploadmedia(file_name)
        media_id = ""
        if response is not None:
            media_id = response['MediaId']
        response = self.webwxsendmsgimg(user_id, media_id)

    async def webwxuploadmedia(self, image_name):
        url = 'https://file2.wx.qq.com/cgi-bin/mmwebwx-bin/webwxuploadmedia?f=json'
        # 计数器
        self.media_count = self.media_count + 1
        # 文件名
        file_name = image_name
        # MIME格式
        # mime_type = application/pdf, image/jpeg, image/png, etc.
        mime_type = mimetypes.guess_type(image_name, strict=False)[0]
        # 微信识别的文档格式，微信服务器应该只支持两种类型的格式。pic和doc
        # pic格式，直接显示。doc格式则显示为文件。
        media_type = 'pic' if mime_type.split('/')[0] == 'image' else 'doc'
        # 上一次修改日期
        lastModifieDate = 'Thu Mar 17 2016 00:55:10 GMT+0800 (CST)'
        lastModifieDate = datetime.now(tz=timezone(timedelta(hours=8))).strftime('%a %b %d %Y %H:%M:%S GMT%z (CST)')

        # 文件大小
        file_size = os.path.getsize(file_name)
        # PassTicket
        pass_ticket = self.pass_ticket
        # clientMediaId
        client_media_id = str(int(time.time() * 1000)) + \
            str(random.random())[:5].replace('.', '')
        # webwx_data_ticket
        webwx_data_ticket = ''
        logger.debug(self.__client.cookie_jar.__dict__['_cookies'])
        cookie = self.__client.cookie_jar.__dict__['_cookies']['qq.com']
        logger.debug(cookie)

        #logger.debug(self.cookie)
        if 'webwx_data_ticket' in cookie:
                webwx_data_ticket = cookie['webwx_data_ticket'].value
        if (webwx_data_ticket == ''):
            return "None Fuck Cookie"

        uploadmediarequest = json.dumps({
            "BaseRequest": self.BaseRequest,
            "ClientMediaId": client_media_id,
            "TotalLen": file_size,
            "StartPos": 0,
            "DataLen": file_size,
            "MediaType": 4
        }).encode('utf8')

        data={
            'id': 'WU_FILE_' + str(self.media_count),
            'type': mime_type,
            'lastModifieDate': lastModifieDate,
            'size': str(file_size),
            'mediatype': media_type,
            'uploadmediarequest': uploadmediarequest,
            'webwx_data_ticket': webwx_data_ticket,
            'pass_ticket': 'undefined', #pass_ticket,
            file_name: open(file_name, 'rb'),
        }
        logger.debug(data)

        headers = {
            'Host': 'file2.wx.qq.com',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.10; rv:42.0) Gecko/20100101 Firefox/42.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Referer': 'https://wx2.qq.com/',
            'Origin': 'https://wx2.qq.com',
            'Connection': 'keep-alive',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache'
        }

        async with self.__client.options(url, headers=headers) as r1:
            response_json1 = await r1.json()
            logger.debug(response_json1)
            if response_json1['BaseResponse']['Ret'] == 1:
                async with self.__client.post(url, data=data, proxy="http://127.0.0.1:8888", headers=headers) as r:
                    response_json = await r.json()
                    logger.debug(response_json)
                    if response_json['BaseResponse']['Ret'] == 0:
                        return response_json
        return None

    def webwxsendmsgimg(self, user_id, media_id):
        url = 'https://wx2.qq.com/cgi-bin/mmwebwx-bin/webwxsendmsgimg?fun=async&f=json&pass_ticket=%s' % self.pass_ticket
        clientMsgId = str(int(time.time() * 1000)) + \
            str(random.random())[:5].replace('.', '')
        data_json = {
            "BaseRequest": self.BaseRequest,
            "Msg": {
                "Type": 3,
                "MediaId": media_id,
                "FromUserName": self.User['UserName'],
                "ToUserName": user_id,
                "LocalID": clientMsgId,
                "ClientMsgId": clientMsgId
            }
        }
        headers = {'content-type': 'application/json; charset=UTF-8'}
        data = json.dumps(data_json, ensure_ascii=False).encode('utf8')
        r = requests.post(url, data=data, headers=headers)
        dic = r.json()
        return dic['BaseResponse']['Ret'] == 0
