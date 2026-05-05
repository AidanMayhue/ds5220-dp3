# ds5220-dp3

https://eqm4hheif3.execute-api.us-east-1.amazonaws.com/api/

# data source

This project is an ingestion pipeline via a chalice API. The pipeline is tracking data from the Steam API, more specifically, the player count for the video game Magic the Gathering Arena. I am a fan of the card game and often play the video game when there aren't others around to play. I enjoy experimenting with the deckbuilding mechanics, however the game has adopted a model where it encourages you to play daily in short bursts. I was curious how this would impact player count. For example whether or not it would drastically fluctuate due to overall shorter sessions. 

# sampling and storage schema

Data is sampled in 15 minute intervals. Data is stored in a DynamoDB table with four attributes, the name of the game (game), which acts as the partition key. Timestamp, which acts as a sort key. Player_count tracks the number of players, appid is the steam id for the game. 

# API resources

API/ returns a short description of the project along with different resources. /plot returns a visualization of player count over the last 24 hours. /current returns the most recent player count. /trend returns average player count and the max player count over the last 24 hours. /compare is intended to compare the player counts of another game to MTG Arena, I did not add another game so this can be ignored.

# stretch goals

I attempted to add more resources for the stretch goal but by the fifth resource I was concerned of how meaningful comparing two game's player counts would be since the playerbase for a FPS is far more likely to be higher compared to a niche strategy card game. I had already mostly completed with the project and didn't want to mess with the endpoint. This would also fall under the category of multisource 
