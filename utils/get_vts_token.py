import pyvts
import asyncio

async def connect_auth(myvts: pyvts.vts):
    ''' functions to get authenticated '''
    await myvts.connect()
    await myvts.request_authenticate_token()
    await myvts.request_authenticate()
    response_data = await myvts.request(myvts.vts_request.requestHotKeyList())
    print(response_data)
    hotkey_list = ['no_extra_action']
    for hotkey in response_data["data"]["availableHotkeys"]:
        hotkey_list.append(hotkey["name"])
    await myvts.close()

myvts = pyvts.vts({"plugin_name": "AI-Vtuber", "developer": "foxabbage", "authentication_token_path": "./tokens/vts_token.txt"})
asyncio.run(connect_auth(myvts))