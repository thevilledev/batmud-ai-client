#!/bin/bash

MONSTERS=("giant rat" "orc warrior" "cave troll" "angry merchant" "confused tourist" "deadly bunny" "sleepy guard")
WEAPONS=("rusty dagger" "wooden stick" "magical banana" "rubber chicken" "enchanted sock" "mysterious spoon")
ACTIONS=("tickles" "confuses" "mildly annoys" "dramatically points at" "throws a pie at" "challenges to a dance-off")
EFFECTS=("and causes mild embarrassment" "but trips in the process" "and runs away giggling" "while humming a tune" "and apologizes immediately" "but forgot why")

generate_message() {
    monster=${MONSTERS[$RANDOM % ${#MONSTERS[@]}]}
    weapon=${WEAPONS[$RANDOM % ${#WEAPONS[@]}]}
    action=${ACTIONS[$RANDOM % ${#ACTIONS[@]}]}
    effect=${EFFECTS[$RANDOM % ${#EFFECTS[@]}]}
    
    echo -e "\033[1;32m>>> BatMUD Combat Log <<<\033[0m"
    echo -e "\033[1;33mA wild $monster appears!\033[0m"
    echo -e "\033[1;36mYou grab your $weapon and $action the $monster, $effect!\033[0m"
    echo -e "\033[1;35m*The nearby NPCs facepalm collectively*\033[0m"
}

# ASCII art banner
cat << "EOF"
 ____        _   __  __ _   _ ____  
| __ )  __ _| |_|  \/  | | | |  _ \ 
|  _ \ / _` | __| |\/| | | | | | | |
| |_) | (_| | |_| |  | | |_| | |_| |
|____/ \__,_|\__|_|  |_|\___/|____/ 
                                    
EOF

generate_message 