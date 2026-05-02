# Read me!
A Capstone project for UNO Class of 2026, In this project we aim to create a mind controlled drone, using a EEG.

## RELEASE 1.0
This is our final release. With it, we have completed our implementation of a BCI (Brain Computer Interface) controlled drone. We have a UI delivering vital information to the usual, and a threaded command and control implementation for command translation. We targeted a modular style, as to help along future groups who may decide to iterate upon this initial release.

## Local devlopment / Install Instructions
Depends on: [Python 3.9+](https://www.python.org/downloads), pip, [Emotiv Launcher](https://www.emotiv.com/emotiv-launcher#download).
The user/developer must get API keys for Emotiv, in order to train or utilize the headset in any capacity.
### Clone and setup venv
```
git clone https://github.com/jalenv119/DRNCTRL_OPEN.git && cd DRNCTRL_OPEN
```
```
python3 -m venv .venv && source .venv/bin/activate
```
```
pip install -r requirements.txt
```
- NOTE: the source command may need to be changed based on the operating system you have, this is written for Unix based operating systems, and assumes a POSIX compliant shell-- E.G. bash, zsh, sh, etc.
### Flow
1. Run Emotiv launcher and sign in; ensure it is running as a background process. (Install the Emotiv BCI package as well for training)
2. Create a user headset profile and generate keys/tokens. If the user desires, they can begin headset training at this point
3. Replace the CLIENT_SECRET, CLIENT_ID, PROFILE within cortex.py. Each of these should have a placeholder currently in the string areas.
4. Run either gui.py or cortex.py to initialize/cache offline keys.
   - Due to how the drone is connected, keys must be cached for offline use.

   - If the user wishes to control the drone, after the initial run, they should connect to the drones SSID after powering it on and rerun.
   - If the user does not wish to operate the drone, they can continue with the initial run without concern.

5. For adding new commands, the user can change the option in the cortex file specifying the json commands (ACTION_TO_OUTPUT).

### Considerations
Consult the Emotiv documentation for any issues that are related to contact/signal quality.
