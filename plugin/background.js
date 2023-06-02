var callback = function (details) {
    var send_headers = {}
    for (var i = 0; i < details.requestHeaders.length; ++i) {
        send_headers[details.requestHeaders[i].name] = details.requestHeaders[i].value
    }
    chrome.storage.sync.get({
        url: "http://127.0.0.1:8080"
    }, function (items) {
        var url = items.url;
        console.log(url);
        fetch(url, { headers: send_headers })
            .then((response) => {
                // console.log(response.ok)
                chrome.action.setIcon({ path: 'images/green.png' });
            })
            .catch((error) => {
                // console.log(error);
                chrome.action.setIcon({ path: 'images/red.png' });
            });
    });
};

var filter = {
    urls: [
        "https://utas.mob.v1.fut.ea.com/*"
        // "https://utas.mob.v1.fut.ea.com/ut/game/fifa23/*"
    ]
};

chrome.webRequest.onSendHeaders.addListener(callback, filter, ['requestHeaders']);
