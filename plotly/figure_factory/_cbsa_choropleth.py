from plotly import colors, exceptions, optional_imports

from plotly.figure_factory import utils

import io
import numpy as np
import os
import pandas as pd
import warnings

pd.options.mode.chained_assignment = None ## TODO:  figure out the purose of this line
shapely = optional_imports.get_module('shapely')
shapefile = optional_imports.get_module('shapefile')
gp = optional_imports.get_module('geopandas')

shape_us_cbsa_2013 = 'tl_2013_us_cbsa.shp'
abs_package_data_dir_path = os.path.join('/Users/mbk/Downloads',
    'tl_2013_us_cbsa')
shape_us_cbsa_2013 = os.path.join(abs_package_data_dir_path,
    'tl_2013_us_cbsa.shp')

df_shape_cbsa_2013 = gp.read_file(shape_us_cbsa_2013)

gdf = gp.GeoDataFrame(data=attributes, geometry=geometry)


def create_choropleth(area_codes, values):
    pass

if __name__ == '__main__':
    print('here')
