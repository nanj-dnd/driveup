// Toolbar click: focus the Driveup tab if it's open, otherwise open one.
chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL("app.html");
  const [tab] = await chrome.runtime.getContexts({ contextTypes: ["TAB"], documentUrls: [url] });
  if (!tab) return chrome.tabs.create({ url });
  await chrome.tabs.update(tab.tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
});
