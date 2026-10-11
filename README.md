# CraftBeerPi 4

[![Build](https://github.com/PiBrewing/craftbeerpi4/actions/workflows/build.yml/badge.svg)](https://github.com/PiBrewing/craftbeerpi4/actions/workflows/build.yml)
[![GitHub license](https://img.shields.io/github/license/PiBrewing/craftbeerpi4)](https://github.com/PiBrewing/craftbeerpi4/blob/master/LICENSE)
![GitHub issues](https://img.shields.io/github/issues-raw/PiBrewing/craftbeerpi4)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/PiBrewing/craftbeerpi4.svg?label=commits)](https://github.com/PiBrewing/craftbeerpi4/commits)
![PyPI](https://img.shields.io/pypi/v/cbpi4)
![Happy Brewing](https://img.shields.io/badge/CraftBeerPi%204-Happy%20Brewing-%23FBB117)

<p align="center">
  <img src="https://github.com/PiBrewing/craftbeerpi4-ui/blob/main/cbpi4gui/public/logo192.png?raw=true" alt="CraftBeerPi Logo"/>
</p>

CraftBeerPi 4 is an open source software solution to control the brewing and
fermentation of beer :beer:.

## 📚 Documentation
Instructions on how to install CraftBeerPi and use its plugins is described
in the documentation, that can be found here: [gitbook.io](https://openbrewing.gitbook.io/craftbeerpi4_support/).

## 📚 Changelog
Changelog can be found [here](./CHANGELOG.md)

### Plugins
Plugins extend the base functionality of CraftBeerPi 4.
You can find a list of available plugins [here](https://openbrewing.gitbook.io/craftbeerpi4_support/master/plugin-installation#plugin-list).

### Brewing timer recovery

MashStep, BoilStep, WaitStep and ActorStep have two per-step settings:

- **RecoveryMode**: `Paused` (default) restores the step without starting its
  timer or equipment logic. `Automatic` resumes a previously active step.
- **RecoveryDowntime**: `No` (default) uses the last saved remaining time.
  `Yes` subtracts application downtime using the saved absolute end time.
  Use this only when the brewing process continues while the application is down.

The working configuration's `step_data.json` is checkpointed every 60 seconds,
and immediately when the timer starts, its duration changes or the step is
paused/completed. Writes use an atomic file replacement. Remaining-time recovery
can repeat approximately one minute of elapsed time; deadline recovery depends
on correct system time (configure clock synchronization on the Raspberry Pi).

A manually paused step always stays paused and does not count downtime.
With `Paused` + `Yes`, downtime is counted up to application startup, then the
restored remaining time is frozen until Start is pressed. Temperature conditions
still apply when resuming MashStep and BoilStep. An already expired timer never
turns on the step's equipment: automatic recovery finishes it, while paused
recovery waits for Start. Previously issued boil hop notifications are preserved;
missed additions are reported as overdue on resumption, including Hop_6.

Orderly shutdown preserves whether the step was active or manually paused.
Older profiles without a checkpoint restore paused with their original duration
and a warning. Timer execution state is not exported into recipes and is cleared
on reset or when loading a different recipe. Fermentation and third-party steps
do not opt into this timer recovery mechanism.

## 🧑‍🤝‍🧑 Contribute
You want to help develop CraftBeerPi4? To get you quickly stated, this repository comes with a preconfigured
development environment. To be able to use this environment you need 2 things installed on your computer:

- docker
- visual studio code (vscode)

To start developing clone this repository, open the folder in vscode and use the _development container_ feature. The command is called _Reopen in container_. Please note that this quick start setup does not work if you want to develop directly on a 32bit raspberry pi os because docker is only available for 64bit arm plattform. Please use the regular development setup for that.

For a more detailed description of a development setup without the _development container_ feature see the documentation page:
[gitbook.io](https://openbrewing.gitbook.io/craftbeerpi4_support/)

### Contributors
Thanks to all the people who have contributed

[![contributors](https://contributors-img.web.app/image?repo=PiBrewing/craftbeerpi4)](https://github.com/PiBrewing/craftbeerpi4/graphs/contributors)
